import inspect
import os
import torch
import pickle
import numpy as np
import pandas as pd

from anndata import AnnData, read
from copy import deepcopy
from typing import Optional, Union

from .expimap import expiMap
from scarches.trainers.expimap.regularized import VIATrainer
from scarches.models.base._utils import _validate_var_names
from scarches.models.base._base import BaseMixin, SurgeryMixin, CVAELatentsMixin


class EXPIMAP(BaseMixin, SurgeryMixin, CVAELatentsMixin):
    """Model for scArches class. This class contains the implementation of Conditional Variational Auto-encoder.
       Parameters
       ----------
       adata: : `~anndata.AnnData`
            Annotated data matrix. Has to be count data for 'nb' and 'zinb' loss and normalized log transformed data
            for 'mse' loss.
       condition_key: String
            column name of conditions in `adata.obs` data frame.
       conditions: List
            List of Condition names that the used data will contain to get the right encoding when used after reloading.
       hidden_layer_sizes: List
            A list of hidden layer sizes for encoder network. Decoder network will be the reversed order.
       latent_dim: Integer
            Bottleneck layer (z)  size.
       dr_rate: Float
            Dropput rate applied to all layers, if `dr_rate`==0 no dropout will be applied.
       recon_loss: String
            Definition of Reconstruction-Loss-Method, 'mse' or 'nb'.
       use_l_encoder: Boolean
            If True and `decoder_last_layer`='softmax', libary size encoder is used.
       use_bn: Boolean
            If `True` batch normalization will be applied to layers.
       use_ln: Boolean
            If `True` layer normalization will be applied to layers.
       mask: Array or List
            if not None, an array of 0s and 1s from utils.add_annotations to create VAE with a masked linear decoder.
       mask_key: String
            A key in `adata.varm` for the mask if the mask is not provided.
       decoder_last_layer: String or None
            The last layer of the decoder. Must be 'softmax' (default for 'nb' loss), identity(default for 'mse' loss),
            'softplus', 'exp' or 'relu'.
    """
    def __init__(
        self,
        adata: AnnData,
        condition_key: str = None,
        conditions: Optional[list] = None,
        hidden_layer_sizes: list = [256, 256],
        dr_rate: float = 0.05,
        recon_loss: str = 'nb',
        use_l_encoder: bool = False,
        use_bn: bool = False,
        use_ln: bool = True,
        mask: Optional[Union[np.ndarray, list]] = None,
        mask_key: str = 'I',
        decoder_last_layer: Optional[str] = None,
        soft_mask: bool = False,
        n_ext: int = 0,
        n_ext_m: int = 0,
        use_hsic: bool = False,
        hsic_one_vs_all: bool = False,
        ext_mask: Optional[torch.Tensor] = None,
        soft_ext_mask: bool = False
    ):
        self.adata = adata

        if mask is None and mask_key not in self.adata.varm:
            raise ValueError('Please provide mask.')

        self.condition_key_ = condition_key

        if conditions is None:
            if condition_key is not None:
                self.conditions_ = adata.obs[condition_key].unique().tolist()
            else:
                self.conditions_ = []
        else:
            self.conditions_ = conditions

        self.hidden_layer_sizes_ = hidden_layer_sizes
        self.dr_rate_ = dr_rate
        self.recon_loss_ = recon_loss
        self.use_bn_ = use_bn
        self.use_ln_ = use_ln

        self.input_dim_ = adata.n_vars

        self.use_l_encoder_ = use_l_encoder
        self.decoder_last_layer_ = decoder_last_layer

        if mask is None:
            mask = adata.varm[mask_key].T

        self.mask_ = mask if isinstance(mask, list) else mask.tolist()
        mask = torch.tensor(mask).float()
        self.latent_dim_ = len(self.mask_)

        self.ext_mask_ = None
        if ext_mask is not None:
            self.ext_mask_ = ext_mask if isinstance(ext_mask, list) else ext_mask.tolist()
            ext_mask = torch.tensor(ext_mask).float()

        self.n_ext_ = n_ext
        self.n_ext_m_ = n_ext_m

        self.soft_mask_ = soft_mask
        self.soft_ext_mask_ = soft_ext_mask

        self.use_hsic_ = use_hsic and n_ext > 0
        self.hsic_one_vs_all_ = hsic_one_vs_all

        self.model = expiMap(
            self.input_dim_,
            self.latent_dim_,
            mask,
            self.conditions_,
            self.hidden_layer_sizes_,
            self.dr_rate_,
            self.recon_loss_,
            self.use_l_encoder_,
            self.use_bn_,
            self.use_ln_,
            self.decoder_last_layer_,
            self.soft_mask_,
            self.n_ext_,
            self.n_ext_m_,
            self.use_hsic_,
            self.hsic_one_vs_all_,
            ext_mask,
            self.soft_ext_mask_
        )

        self.is_trained_ = False

        self.trainer = None

    def train(
        self,
        n_epochs: int = 400,
        lr: float = 1e-3,
        eps: float = 0.01,
        alpha: Optional[float] = None,
        omega: Optional[torch.Tensor] = None,
        **kwargs
    ):
        """Train the model.
           Parameters
           ----------
           n_epochs
                Number of epochs for training the model.
           lr
                Learning rate for training the model.
           eps
                torch.optim.Adam eps parameter
           kwargs
                kwargs for the TrVAE trainer.
        """
        self.trainer = VIATrainer(
            self.model,
            self.adata,
            alpha=alpha,
            omega=omega,
            condition_key=self.condition_key_,
            **kwargs
        )
        self.trainer.train(n_epochs, lr, eps)
        self.is_trained_ = True

    def nonzero_terms(self):
        return self.model.decoder.nonzero_terms()

    def terms_genes(self, terms: Union[str, list]='terms'):
        if isinstance(terms, str):
            terms = self.adata.uns[terms]
        else:
            if len(terms) != len(self.mask_):
                raise ValueError('The list of terms should have the same length as the mask.')
        I = np.array(self.mask_, dtype=bool)
        return {term: self.adata.var_names[I[i]].tolist() for i, term in enumerate(terms)}

    def latent_directions(self, method="sum", get_confidence=False,
                          adata=None, key_added='directions'):
        if adata is None:
            adata = self.adata

        terms_weights = self.model.decoder.L0.expr_L.weight.data

        if method == "sum":
            signs = terms_weights.sum(0).cpu().numpy()
            signs[signs>0] = 1.
            signs[signs<0] = -1.
            confidence = None
        elif method == "counts":
            num_nz = torch.count_nonzero(terms_weights, dim=0)
            upreg_genes = torch.count_nonzero(terms_weights > 0, dim=0)
            signs = upreg_genes / (num_nz+(num_nz==0))
            signs = signs.cpu().numpy()

            confidence = signs.copy()
            confidence = np.abs(confidence-0.5)/0.5
            confidence[num_nz==0] = 0

            signs[signs>0.5] = 1.
            signs[signs<0.5] = -1.

            signs[signs==0.5] = 0
            signs[num_nz==0] = 0
        else:
            raise ValueError("Unrecognized method for getting the latent direction.")

        adata.uns[key_added] = signs
        if get_confidence and confidence is not None:
            adata.uns[key_added + '_confindence'] = confidence

    def latent_enrich(
        self,
        groups,
        comparison='rest',
        n_sample=5000,
        use_directions=False,
        directions_key='directions',
        select_terms=None,
        adata=None,
        exact=True,
        key_added='bf_scores'
    ):
        if adata is None:
            adata = self.adata

        if isinstance(groups, str):
            cats_col = adata.obs[groups]
            cats = cats_col.unique()
        elif isinstance(groups, dict):
            cats = []
            all_cells = []
            for group, cells in groups.items():
                cats.append(group)
                all_cells += cells
            adata = adata[all_cells]
            cats_col = pd.Series(index=adata.obs_names, dtype=str)
            for group, cells in groups.items():
                cats_col[cells] = group
        else:
            raise ValueError("groups should be a string or a dict.")

        if comparison != "rest" and set(comparison).issubset(cats):
            raise ValueError("comparison should be 'rest' or among the passed groups")

        scores = {}

        if comparison != "rest" and isinstance(comparison, str):
            comparison = [comparison]

        for cat in cats:
            if cat in comparison:
                continue

            cat_mask = cats_col == cat
            if comparison == "rest":
                others_mask = ~cat_mask
            else:
                others_mask = cats_col.isin(comparison)

            choice_1 = np.random.choice(cat_mask.sum(), n_sample)
            choice_2 = np.random.choice(others_mask.sum(), n_sample)

            adata_cat = adata[cat_mask][choice_1]
            adata_others = adata[others_mask][choice_2]

            if use_directions:
                directions = adata.uns[directions_key]
            else:
                directions = None

            z0 = self.get_latent(
                adata_cat.X,
                adata_cat.obs[self.condition_key_],
                mean=False,
                mean_var=exact
            )
            z1 = self.get_latent(
                adata_others.X,
                adata_others.obs[self.condition_key_],
                mean=False,
                mean_var=exact
            )

            if not exact:
                if directions is not None:
                    z0 *= directions
                    z1 *= directions

                if select_terms is not None:
                    z0 = z0[:, select_terms]
                    z1 = z1[:, select_terms]

                to_reduce = z0 > z1

                zeros_mask = (np.abs(z0).sum(0) == 0) | (np.abs(z1).sum(0) == 0)
            else:
                from scipy.special import erfc

                means0, vars0 = z0
                means1, vars1 = z1

                if directions is not None:
                    means0 *= directions
                    means1 *= directions

                if select_terms is not None:
                    means0 = means0[:, select_terms]
                    means1 = means1[:, select_terms]
                    vars0 = vars0[:, select_terms]
                    vars1 = vars1[:, select_terms]

                to_reduce = (means1 - means0) / np.sqrt(2 * (vars0 + vars1))
                to_reduce = 0.5 * erfc(to_reduce)

                zeros_mask = (np.abs(means0).sum(0) == 0) | (np.abs(means1).sum(0) == 0)

            p_h0 = np.mean(to_reduce, axis=0)
            p_h1 = 1.0 - p_h0
            epsilon = 1e-12
            bf = np.log(p_h0 + epsilon) - np.log(p_h1 + epsilon)

            p_h0[zeros_mask] = 0
            p_h1[zeros_mask] = 0
            bf[zeros_mask] = 0

            scores[cat] = dict(p_h0=p_h0, p_h1=p_h1, bf=bf)

        adata.uns[key_added] = scores

    @classmethod
    def load_query_data(
        cls,
        adata: AnnData,
        reference_model: Union[str, 'TRVAE'],
        freeze: bool = True,
        freeze_expression: bool = True,
        unfreeze_ext: bool = True,
        remove_dropout: bool = True,
        new_n_ext: Optional[int] = None,
        new_n_ext_m: Optional[int] = None,
        new_ext_mask: Optional[Union[np.ndarray, list]] = None,
        new_soft_ext_mask: bool = False
    ):
        params = {}
        params['adata'] = adata
        params['reference_model'] = reference_model
        params['freeze'] = freeze
        params['freeze_expression'] = freeze_expression
        params['remove_dropout'] = remove_dropout

        if new_n_ext is not None:
            params['n_ext'] = new_n_ext
        if new_n_ext_m is not None:
            params['n_ext_m'] = new_n_ext_m
            if new_ext_mask is None:
                raise ValueError('Provide new ext_mask')
            params['ext_mask'] = new_ext_mask
            params['soft_ext_mask'] = new_soft_ext_mask

        new_model = super().load_query_data(**params)

        if freeze and unfreeze_ext:
            for name, p in new_model.model.named_parameters():
                if 'ext_L.weight' in name or 'ext_L_m.weight' in name:
                    p.requires_grad = True
                if 'expand_mean_encoder' in name or 'expand_var_encoder' in name:
                    p.requires_grad = True

        return new_model

    @classmethod
    def _get_init_params_from_dict(cls, dct):
        init_params = {
            'condition_key': dct['condition_key_'],
            'conditions': dct['conditions_'],
            'hidden_layer_sizes': dct['hidden_layer_sizes_'],
            'dr_rate': dct['dr_rate_'],
            'recon_loss': dct['recon_loss_'],
            'use_bn': dct['use_bn_'],
            'use_ln': dct['use_ln_'],
            'mask': dct['mask_'],
            'decoder_last_layer': dct['decoder_last_layer_'] if 'decoder_last_layer_' in dct else "softmax",
            'use_l_encoder': dct['use_l_encoder_'] if 'use_l_encoder_' in dct else False,
            'n_ext': dct['n_ext_'] if 'n_ext_' in dct else 0,
            'n_ext_m': dct['n_ext_m_'] if 'n_ext_m_' in dct else 0,
            'soft_mask': dct['soft_mask_'] if 'soft_mask_' in dct else False,
            'soft_ext_mask': dct['soft_ext_mask_'] if 'soft_ext_mask_' in dct else False,
            'hsic_one_vs_all': dct['hsic_one_vs_all_'] if 'hsic_one_vs_all_' in dct else False,
            'use_hsic': dct['use_hsic_'] if 'use_hsic_' in dct else False,
            'ext_mask': dct['ext_mask_'] if 'ext_mask_' in dct else None
        }

        return init_params

    @classmethod
    def _validate_adata(cls, adata, dct):
        if adata.n_vars != dct['input_dim_']:
            raise ValueError("Incorrect var dimension")

        adata_conditions = adata.obs[dct['condition_key_']].unique().tolist()
        if not set(adata_conditions).issubset(dct['conditions_']):
            raise ValueError("Incorrect conditions")
