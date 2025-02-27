# Copyright 2022 MosaicML Diffusion authors
# SPDX-License-Identifier: Apache-2.0

"""Streaming Image-Caption dataset."""

import logging
import random
from io import BytesIO
from typing import Callable, Dict, List, Optional, Sequence, Union

import torch
from PIL import Image
from streaming import Stream, StreamingDataset
from torch.utils.data import DataLoader
from torchvision import transforms
from transformers import AutoTokenizer

from diffusion.datasets.laion.transforms import LargestCenterSquare, RandomCropAspectRatioTransorm, RandomCropSquare
from diffusion.models.models import SDXLTokenizer

log = logging.getLogger(__name__)

# Disable PIL max image size limit
Image.MAX_IMAGE_PIXELS = None


class StreamingImageCaptionDataset(StreamingDataset):
    """Streaming dataset for image-caption pairs.

    Args:
        streams (Sequence[Stream], optional): One or more Streams to stream/cache samples from.
            ``StreamingImageCaptionDataset`` uses either ``streams`` or ``remote``/``local``. Default:``None``.
        remote (str, optional): Remote directory (S3 or local filesystem) where dataset is stored. Default: ``None``.
        local (str, optional): Local filesystem directory where dataset is cached during operation. Default: ``None``.
        tokenizer_name_or_path (str): The name or path of the tokenizer to use. Default: ``'stabilityai/stable-diffusion-2-base'``.
        caption_drop_prob (float): The probability of dropping a caption. Default: ``0.0``.
        microcond_drop_prob (float): The probability of dropping microconditioning. Only relevant for SDXL. Default: ``0.0``.
        caption_selection (str): If there are multiple captions, specifies how to select a single caption.
            'first' selects the first caption in the list and 'random' selects a random caption in the list.
            If there is only one caption, this argument is ignored. Default: ``'first'``.
        crop (Callable, optional): The crop transform to apply to the image before ``transform``. Default: ``None``
        transform (Callable, optional): The transforms to apply to the image. Default: ``None``.
        image_key (str): Key associated with the image in the streaming dataset. Default: ``'image'``.
        caption_key (str): Key associated with the caption in the streaming dataset. Default: ``'caption'``.
        sdxl (bool): Whether or not we're training SDXL. Default: `False`.
        zero_dropped_captions (bool): If True, zero out text embeddings for dropped captions. Default: ``False``.

        **streaming_kwargs: Additional arguments to pass in the construction of the StreamingDataloader
    """

    def __init__(
        self,
        streams: Optional[Sequence[Stream]] = None,
        remote: Optional[str] = None,
        local: Optional[str] = None,
        tokenizer_name_or_path: str = 'stabilityai/stable-diffusion-2-base',
        caption_drop_prob: float = 0.0,
        microcond_drop_prob: float = 0.0,
        caption_selection: str = 'first',
        crop: Optional[Callable] = None,
        transform: Optional[Callable] = None,
        image_key: str = 'image',
        caption_key: str = 'caption',
        sdxl: bool = False,
        zero_dropped_captions: bool = False,
        **streaming_kwargs,
    ) -> None:

        # Set defaults for vision-friendly streaming args.
        streaming_kwargs.setdefault('shuffle_block_size', 1 << 18)
        streaming_kwargs.setdefault('shuffle_algo', 'py1s')

        super().__init__(
            streams=streams,
            remote=remote,
            local=local,
            **streaming_kwargs,
        )
        caption_selection = caption_selection.lower()
        if caption_selection not in ['first', 'random']:
            raise ValueError(f'Invalid caption selection: {caption_selection}. Must be one of [random, first]')

        self.crop = crop
        self.transform = transform
        self.sdxl = sdxl
        self.caption_drop_prob = caption_drop_prob
        self.microcond_drop_prob = microcond_drop_prob
        self.caption_selection = caption_selection
        self.image_key = image_key
        self.caption_key = caption_key
        self.zero_dropped_captions = zero_dropped_captions

        if self.sdxl:
            self.tokenizer = SDXLTokenizer(tokenizer_name_or_path)
        else:
            self.tokenizer = AutoTokenizer.from_pretrained(tokenizer_name_or_path, subfolder='tokenizer')

    def __getitem__(self, index):
        sample = super().__getitem__(index)
        out = {}

        # Image
        img = sample[self.image_key]
        if not isinstance(img, Image.Image):
            img = Image.open(BytesIO(sample[self.image_key]))
        if img.mode != 'RGB':
            img = img.convert('RGB')
        orig_w, orig_h = img.size

        # Image transforms
        if self.crop is not None:
            img, crop_top, crop_left = self.crop(img)
        else:
            crop_top, crop_left = 0, 0
        if self.transform is not None:
            img = self.transform(img)
        out['image'] = img

        # SDXL microconditioning on image characteristics
        if self.sdxl:
            # Get the new height and width
            if isinstance(img, torch.Tensor):
                img_h, img_w = img.shape[-2], img.shape[-1]
            elif isinstance(img, Image.Image):
                img_w, img_h = img.size
            else:
                raise ValueError('Image after transformations must either be a PIL Image or Torch Tensor')

            out['cond_crops_coords_top_left'] = torch.tensor([crop_top, crop_left])
            out['cond_original_size'] = torch.tensor([orig_w, orig_h])
            out['cond_target_size'] = torch.tensor([img_w, img_h])

            # Microconditioning dropout as in Stability repo
            # https://github.com/Stability-AI/generative-models/blob/477d8b9a7730d9b2e92b326a770c0420d00308c9/sgm/modules/encoders/modules.py#L151-L160
            if torch.rand(1) < self.microcond_drop_prob:
                out['cond_crops_coords_top_left'] = out['cond_crops_coords_top_left'] * 0
            if torch.rand(1) < self.microcond_drop_prob:
                out['cond_original_size'] = out['cond_original_size'] * 0
            if torch.rand(1) < self.microcond_drop_prob:
                out['cond_target_size'] = out['cond_target_size'] * 0

        # Caption
        if torch.rand(1) < self.caption_drop_prob:
            caption = ''
            if self.zero_dropped_captions:
                out['drop_caption_mask'] = 0.0
            else:
                out['drop_caption_mask'] = 1.0
        else:
            caption = sample[self.caption_key]
            if isinstance(caption, List) and self.caption_selection == 'first':
                caption = caption[0]
            if isinstance(caption, List) and self.caption_selection == 'random':
                caption = random.sample(caption, k=1)[0]
            out['drop_caption_mask'] = 1.0

        max_length = None if self.sdxl else self.tokenizer.model_max_length  # type: ignore
        tokenizer_out = self.tokenizer(caption,
                                       padding='max_length',
                                       max_length=max_length,
                                       truncation=True,
                                       return_tensors='pt')
        if self.sdxl:
            tokenized_caption = [tokenized_cap.squeeze() for tokenized_cap in tokenizer_out.input_ids]
            tokenized_caption = torch.stack(tokenized_caption)
            # Take union over both tokenizers padding masks
            attention_masks = tokenizer_out.attention_mask
            attention_mask = torch.logical_or(attention_masks[0], attention_masks[1]).to(attention_masks[0].dtype)
        else:
            tokenized_caption = tokenizer_out.input_ids.squeeze()
            attention_mask = tokenizer_out.attention_mask
        out['captions'] = tokenized_caption
        out['attention_mask'] = attention_mask
        return out


def build_streaming_image_caption_dataloader(
    remote: Union[str, List],
    local: Union[str, List],
    batch_size: int,
    tokenizer_name_or_path: str = 'stabilityai/stable-diffusion-2-base',
    caption_drop_prob: float = 0.0,
    microcond_drop_prob: float = 0.0,
    resize_size: int = 256,
    caption_selection: str = 'first',
    transform: Optional[List[Callable]] = None,
    image_key: str = 'image',
    caption_key: str = 'caption',
    crop_type: Optional[str] = 'square',
    zero_dropped_captions: bool = True,
    streaming_kwargs: Optional[Dict] = None,
    dataloader_kwargs: Optional[Dict] = None,
):
    """Builds a streaming LAION dataloader.

    Args:
        remote (str, Sequence[str]): One or more remote directories (S3 or local filesystem) where dataset is stored.
        local (str, Sequence[str]): One or more local filesystem directories where dataset is cached during operation.
        batch_size (int): The batch size to use for both the ``StreamingDataset`` and ``DataLoader``.
        tokenizer_name_or_path (str): The name or path of the tokenizer to use. Default: ``'stabilityai/stable-diffusion-2-base'``.
        caption_drop_prob (float): The probability of dropping a caption. Default: ``0.0``.
        microcond_drop_prob (float): The probability of dropping microconditioning. Only relevant for SDXL. Default: ``0.0``.
        resize_size (int): The size to resize the image to. Default: ``256``.
        caption_selection (str): If there are multiple captions, specifies how to select a single caption.
            'first' selects the first caption in the list and 'random' selects a random caption in the list.
            If there is only one caption, this argument is ignored. Default: ``'first'``.
        transform (Optional[Callable]): The transforms to apply to the image. Default: ``None``.
        image_key (str): Key associated with the image in the streaming dataset. Default: ``'image'``.
        caption_key (str): Key associated with the caption in the streaming dataset. Default: ``'caption'``.
        crop_type (str, optional): Type of crop to perform, either ['square', 'random', 'aspect_ratio']. Default: ``'square'``.
        zero_dropped_captions (bool): If True, zero out text embeddings for dropped captions. Default: ``True``.
        streaming_kwargs (dict, optional): Additional arguments to pass to the ``StreamingDataset``. Default: ``None``.
        dataloader_kwargs (dict, optional): Additional arguments to pass to the ``DataLoader``. Default: ``None``.
    """
    # Check crop type
    if crop_type is not None:
        crop_type = crop_type.lower()
        if crop_type not in ['square', 'random', 'aspect_ratio']:
            raise ValueError(f'Invalid crop_type: {crop_type}. Must be ["square", "random", "aspect_ratio", None]')

    # Handle ``None`` kwargs
    if streaming_kwargs is None:
        streaming_kwargs = {}
    if dataloader_kwargs is None:
        dataloader_kwargs = {}

    # Check types for remote and local
    if isinstance(remote, str) and isinstance(local, str):
        # Hacky... make remote and local lists to simplify downstream code
        remote, local = [remote], [local]
    elif isinstance(remote, Sequence) and isinstance(local, Sequence):
        if len(remote) != len(local):
            ValueError(
                f'remote and local Sequences must be the same length, got lengths {len(remote)} and {len(local)}')
    else:
        ValueError(f'remote and local must be both Strings or Sequences, got types {type(remote)} and {type(local)}.')

    # Create a Stream for each (remote, local) pair
    streams = []
    for r, l in zip(remote, local):
        streams.append(Stream(remote=r, local=l))

    # Infer SDXL from tokenizer path
    sdxl = (tokenizer_name_or_path == 'stabilityai/stable-diffusion-xl-base-1.0')
    if sdxl:
        log.info('Detected SDXL tokenizer, using SDXL crop transform and tokenizers.')

    # Set the crop to apply
    if crop_type == 'square':
        crop = LargestCenterSquare(resize_size)
    elif crop_type == 'random':
        crop = RandomCropSquare(resize_size)
    elif crop_type == 'aspect_ratio':
        crop = RandomCropAspectRatioTransorm()
    else:
        crop = None

    if transform is None:
        transform = [transforms.ToTensor(), transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))]
    transform = transforms.Compose(transform)
    assert isinstance(transform, Callable)

    dataset = StreamingImageCaptionDataset(
        streams=streams,
        tokenizer_name_or_path=tokenizer_name_or_path,
        caption_drop_prob=caption_drop_prob,
        microcond_drop_prob=microcond_drop_prob,
        caption_selection=caption_selection,
        crop=crop,
        transform=transform,
        image_key=image_key,
        caption_key=caption_key,
        batch_size=batch_size,
        sdxl=sdxl,
        zero_dropped_captions=zero_dropped_captions,
        **streaming_kwargs,
    )

    dataloader = DataLoader(
        dataset=dataset,
        batch_size=batch_size,
        sampler=None,
        **dataloader_kwargs,
    )

    return dataloader
