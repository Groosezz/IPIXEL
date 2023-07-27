from typing import Any, Callable, Iterator
import os
from os import PathLike

import torch
from torch import nn
from torch.nn.parallel import DistributedDataParallel

from pixel import PIXELForPreTraining, PIXELEmbeddings
from pixel import PIXELConfig, PIXELForPreTrainingOutput, PIXELForSequenceClassification
from transformers.configuration_utils import PretrainedConfig
from transformers.utils import logging
from transformers.modeling_outputs import SequenceClassifierOutput
from transformers.models.vit.modeling_vit import ViTEmbeddings

from LatentPixel.text_graph import TGraph

from ..text_graph import TGraph
from .latent_model import LatentModel

logger = logging.get_logger(__name__)


class LPixelForMLM(LatentModel):
    
    def load_backbone(self, path: str | PathLike) -> nn.Module:
        if path is None or len(path) == 0:
            print('No backbone')
            return None
        pixel_config = PIXELConfig.from_pretrained(path)
        pixel_config.image_size = self.latent_size[1:]
        pixel_config.patch_size = self.latent_size[1]
        pixel_config.num_channels = self.latent_size[0]
        pixel_config.norm_pix_loss = False
        self.backbone_config = pixel_config
        self.backbone: PIXELForPreTraining = PIXELForPreTraining.from_pretrained(path, config=pixel_config, ignore_mismatched_sizes=True)
        print('Set the forward mode of PIXEL embedding to gen')
        self.backbone.vit.embeddings.forward_mode = 'gen'
        return self.backbone
    
    def save_backbone(self, path: str | PathLike) -> None:
        if isinstance(self.backbone, PIXELForPreTraining):
            self.backbone.save_pretrained(path)
        elif isinstance(self.backbone, DistributedDataParallel):
            self.backbone.module.save_pretrained(path)
        else:
            raise NotImplementedError(f'Saving for {type(self.backbone)} has not been implemented!')
        
        print(f'PIXEL backbone saved!')
    
    def latent_forward(self, img: TGraph) -> TGraph:
        pixel_values = self._latent_norm(img.value) if self.coder is not None else img.to_pixel()
        output: PIXELForPreTrainingOutput = self.backbone(
            pixel_values=pixel_values,
            attention_mask=img.attention_mask,
            patch_mask=img.patch_mask
            )
        if self.coder is None:
            return TGraph.from_pixel(output, True, patch_size=self.latent_patch_size).unsquarelize()
        
        if isinstance(self.backbone, PIXELForPreTraining):
            logits = self.backbone.unpatchify(output.logits)
            logits = self._inv_latent_norm(logits)
        else:
            logits = self.backbone.module.unpatchify(output.logits)
            logits = self._inv_latent_norm(logits)

        return TGraph.from_value(
            value=logits,
            attention_mask=img.attention_mask,
            patch_mask=output.mask,
            num_text_patches=img.num_text_patches,
            loss=output.loss,
            patch_size=self.latent_patch_size
        ).unsquarelize()
    
    def get_connection_layers(self) -> nn.Module:
        return nn.ModuleList([
            self.backbone.vit.embeddings,
            self.backbone.decoder.decoder_pred
        ])
    
    def init_connection_layers(self) -> None:
        print('Reinitialize the connection layers for the latent pixel')
        self.backbone.vit.embeddings = PIXELEmbeddings(self.backbone_config)
        self.backbone.vit.embeddings.forward_mode = 'gen'
        self.backbone.decoder.decoder_pred = nn.Linear(
            self.backbone_config.decoder_hidden_size,
            self.latent_size[1] ** 2 * self.latent_size[0], 
            bias=True
        )

    def delete_unused_layers(self) -> None:
        if self.coder is not None:
            del self.coder.decoder
            self.coder.decoder = None
            print('The decoder of the coder is deleted')
        else:
            print('There is no coder for this model, skip the deletion')
        
class LPixelForClassification(LatentModel):

    def load_backbone(self, path: str | PathLike) -> nn.Module:
        config = PIXELConfig.from_pretrained(path)
        config.image_size = self.latent_size[1:]
        config.patch_size = self.latent_size[1]
        config.num_channels = self.latent_size[0]
        config.norm_pix_loss = False
        # setattr(config, 'num_labels', self.num_labels)
        config.num_labels = self.num_labels
        pixel: PIXELForSequenceClassification = PIXELForSequenceClassification.from_pretrained(path, config=config, ignore_mismatched_sizes=True)
        self.backbone: PIXELForSequenceClassification = pixel
        if self.coder:
            print('delete the decoder')
            del self.coder.decoder
            self.coder.decoder = None
        return pixel
    
    def save_backbone(self, path: str | PathLike) -> None:
        if isinstance(self.backbone, PIXELForSequenceClassification):
            self.backbone.save_pretrained(path)
        elif isinstance(self.backbone, DistributedDataParallel):
            self.backbone.module.save_pretrained(path)
        else:
            raise NotImplementedError(f'Saving for {type(self.backbone)} has not been implemented!')
        
        print(f'PIXEL backbone saved to {path}')    

    def latent_forward(self, img: TGraph) -> TGraph:
        assert self.coder is None or self.coder.decoder is None, 'no decoder for the classification model'
        values = img.value if self.coder is None else self._latent_norm(img.value)
        output: SequenceClassifierOutput = self.backbone.forward(
            pixel_values=values,
            attention_mask=img.attention_mask,
            labels=img.labels
        )

        result = TGraph()
        result._value = output.logits
        result.loss = output.loss
        result.labels = img.labels
        
        return result
    
    def get_connection_layers(self) -> nn.Module:
        return nn.ModuleList([
            self.backbone.vit.embeddings
        ])
    
    def init_connection_layers(self) -> None:
        print('Reinitialize the vit embedding layer')
        self.backbone.vit.embeddings = ViTEmbeddings(self.backbone.config)
        return
