# Ultralytics YOLO 🚀, AGPL-3.0 license


from copy import copy, deepcopy

import torch

from ultralytics.data import YOLOConcatDataset, build_grounding, build_yolo_dataset
from ultralytics.data.utils import check_det_dataset
from ultralytics.models.yolo.detect import DetectionTrainer, DetectionValidator
from ultralytics.nn.tasks import YOLOEModel
from ultralytics.utils import DEFAULT_CFG, RANK
from ultralytics.utils.torch_utils import de_parallel
from pathlib import Path
from collections import defaultdict

from .val import YOLOEDetectValidator


class YOLOETrainer(DetectionTrainer):
    """A base trainer for YOLOE training."""

    def __init__(self, cfg=DEFAULT_CFG, overrides=None, _callbacks=None):
        if overrides is None:
            overrides = {}
        super().__init__(cfg, overrides, _callbacks)

    def get_model(self, cfg=None, weights=None, verbose=True):
        """Return YOLOEModel initialized with specified config and weights."""
        # NOTE: This `nc` here is the max number of different text samples in one image, rather than the actual `nc`.
        # NOTE: Following the official config, nc hard-coded to 80 for now.
        model = YOLOEModel(
            cfg["yaml_file"] if isinstance(cfg, dict) else cfg,
            ch=3,
            nc=min(self.data["nc"], 80),
            verbose=verbose and RANK == -1,
        )
        if weights:
            model.load(weights)

        return model

    def get_validator(self):
        """Returns a DetectionValidator for YOLO model validation."""
        self.loss_names = "box", "cls", "dfl"
        return YOLOEDetectValidator(
            self.test_loader, save_dir=self.save_dir, args=copy(self.args), _callbacks=self.callbacks
        )

    def build_dataset(self, img_path, mode="train", batch=None):
        """
        Build YOLO Dataset.

        Args:
            img_path (str): Path to the folder containing images.
            mode (str): `train` mode or `val` mode, users are able to customize different augmentations for each mode.
            batch (int, optional): Size of batches, this is for `rect`. Defaults to None.
        """
        gs = max(int(de_parallel(self.model).stride.max() if self.model else 0), 32)
        return build_yolo_dataset(
            self.args, img_path, batch, self.data, mode=mode, rect=mode == "val", stride=gs, multi_modal=mode == "train"
        )

    def preprocess_batch(self, batch):
        batch = super().preprocess_batch(batch)
        batch["txt_feats"] = batch["text_feats"].to(self.device)
        return batch


class YOLOEPETrainer(DetectionTrainer):
    """Fine-tune YOLOE model in linear probing way."""

    def get_model(self, cfg=None, weights=None, verbose=True):
        """Return YOLOEModel initialized with specified config and weights."""
        # NOTE: This `nc` here is the max number of different text samples in one image, rather than the actual `nc`.
        # NOTE: Following the official config, nc hard-coded to 80 for now.
        model = YOLOEModel(
            cfg["yaml_file"] if isinstance(cfg, dict) else cfg,
            ch=3,
            nc=self.data["nc"],
            verbose=verbose and RANK == -1,
        )

        del model.model[-1].savpe

        if weights:
            model.load(weights)

        model.eval()
        # TODO: removed `train_pe_path`
        pe_state = torch.load(self.args.train_pe_path)
        model.set_classes(pe_state["names"], pe_state["pe"])
        model.model[-1].fuse(model.pe)
        model.model[-1].cv3[0][2] = deepcopy(model.model[-1].cv3[0][2]).requires_grad_(True)
        model.model[-1].cv3[1][2] = deepcopy(model.model[-1].cv3[1][2]).requires_grad_(True)
        model.model[-1].cv3[2][2] = deepcopy(model.model[-1].cv3[2][2]).requires_grad_(True)
        del model.pe
        model.train()

        return model


class YOLOETrainerFromScratch(YOLOETrainer):
    """Train YOLOE models from scratch."""

    def __init__(self, cfg=DEFAULT_CFG, overrides=None, _callbacks=None):
        if overrides is None:
            overrides = {}
        super().__init__(cfg, overrides, _callbacks)

    def build_dataset(self, img_path, mode="train", batch=None):
        """
        Build YOLO Dataset for training or validation.

        This method constructs appropriate datasets based on the mode and input paths, handling both
        standard YOLO datasets and grounding datasets with different formats.

        Args:
            img_path (List[str] | str): Path to the folder containing images or list of paths.
            mode (str): 'train' mode or 'val' mode, allowing customized augmentations for each mode.
            batch (int, optional): Size of batches, used for rectangular training/validation.

        Returns:
            (YOLOConcatDataset | Dataset): The constructed dataset for training or validation.
        """
        gs = max(int(de_parallel(self.model).stride.max() if self.model else 0), 32)
        if mode != "train":
            return build_yolo_dataset(
                self.args, img_path, batch, self.data, mode=mode, rect=False, stride=gs, load_vp=False
            )
        datasets = [
            build_yolo_dataset(self.args, im_path, batch, self.training_data[im_path], stride=gs, multi_modal=True)
            if isinstance(im_path, str)
            else build_grounding(self.args, im_path["img_path"], im_path["json_file"], batch, stride=gs)
            for im_path in img_path
        ]

        # TODO: open up an interface to determine whether to do cache
        category_names = set()
        for dataset in datasets:
            if not hasattr(dataset, "category_names"):
                continue
            category_names |= dataset.category_names

        category_freq = defaultdict(int)
        for dataset in datasets:
            if not hasattr(dataset, "category_freq"):
                continue
            for k, v in dataset.category_freq.items():
                category_freq[k] += v
        neg_names = self._get_neg_texts(category_freq, threshold=100)

        # TODO: enable to update the path or use a more general way to get the path
        # TODO: fix: close_mosaic would invalidate this
        img_path = datasets[0].img_path
        pos_embeddings = self.generate_data_embeddings(
            category_names, batch, device=self.device, cache_path=Path(img_path).parent / "pos_embeddings.pt"
        )
        neg_embeddings = self.generate_data_embeddings(
            neg_names, batch, device=self.device, cache_path=Path(img_path).parent / "neg_embeddings.pt"
        )
        for dataset in datasets:
            for i, transform in enumerate(dataset.transforms.transforms):
                if not hasattr(transform, "set_embeddings"):  # use `0` index as transform is a "Compose" object
                    continue
                dataset.transforms.transforms[i].set_embeddings(pos_embeddings, neg_embeddings)

        return YOLOConcatDataset(datasets) if len(datasets) > 1 else datasets[0]

    @staticmethod
    def generate_data_embeddings(texts, batch, device="cuda", cache_path="embeddings.pt"):
        if cache_path.exists():
            return torch.load(cache_path)
        from ultralytics.nn.text_model import build_text_model
        from tqdm import tqdm

        # TODO: hardcode to mobileclip:blt for now
        model = build_text_model("mobileclip:blt", device=device)
        text_tokens = model.tokenize(texts)
        txt_feats = []
        for text_token in tqdm(text_tokens.split(batch)):
            txt_feats.append(model.encode_text(text_token))
        txt_feats = torch.cat(txt_feats, dim=0).cpu()
        txt_map = {text: feat for text, feat in zip(texts, txt_feats)}
        torch.save(txt_map, cache_path)
        return txt_map

    @staticmethod
    def _get_neg_texts(category_freq, threshold=100):
        return [k for k, v in category_freq.items() if v >= threshold]

    def get_dataset(self):
        """
        Get train and validation paths from data dictionary.

        Processes the data configuration to extract paths for training and validation datasets,
        handling both YOLO detection datasets and grounding datasets.

        Returns:
            (str): Train dataset path.
            (str): Validation dataset path.

        Raises:
            AssertionError: If train or validation datasets are not found, or if validation has multiple datasets.
        """
        final_data = {}
        data_yaml = self.args.data
        assert data_yaml.get("train", False), "train dataset not found"  # object365.yaml
        assert data_yaml.get("val", False), "validation dataset not found"  # lvis.yaml
        data = {k: [check_det_dataset(d) for d in v.get("yolo_data", [])] for k, v in data_yaml.items()}
        assert len(data["val"]) == 1, f"Only support validating on 1 dataset for now, but got {len(data['val'])}."
        val_split = "minival" if "lvis" in data["val"][0]["val"] else "val"
        for d in data["val"]:
            if d.get("minival") is None:  # for lvis dataset
                continue
            d["minival"] = str(d["path"] / d["minival"])
        for s in ["train", "val"]:
            final_data[s] = [d["train" if s == "train" else val_split] for d in data[s]]
            # save grounding data if there's one
            grounding_data = data_yaml[s].get("grounding_data")
            if grounding_data is None:
                continue
            grounding_data = grounding_data if isinstance(grounding_data, list) else [grounding_data]
            for g in grounding_data:
                assert isinstance(g, dict), f"Grounding data should be provided in dict format, but got {type(g)}"
            final_data[s] += grounding_data
        # NOTE: to make training work properly, set `nc` and `names`
        final_data["nc"] = data["val"][0]["nc"]
        final_data["names"] = data["val"][0]["names"]
        # NOTE: add path with lvis path
        final_data["path"] = data["val"][0]["path"]
        self.data = final_data
        self.training_data = {}
        for d in data["train"]:
            self.training_data[d["train"]] = d
        return final_data["train"], final_data["val"][0]

    def plot_training_labels(self):
        """Do not plot labels for YOLO-World training."""
        pass

    def final_eval(self):
        val = self.args.data["val"]["yolo_data"][0]
        self.validator.args.data = val
        self.validator.args.split = "minival" if isinstance(val, str) and "lvis" in val else "val"
        return super().final_eval()


class YOLOEPEFreeTrainer(YOLOEPETrainer, YOLOETrainerFromScratch):
    """Train prompt-free YOLOE model."""

    def get_validator(self):
        """Returns a DetectionValidator for YOLO model validation."""
        self.loss_names = "box", "cls", "dfl"
        return DetectionValidator(
            self.test_loader, save_dir=self.save_dir, args=copy(self.args), _callbacks=self.callbacks
        )

    def preprocess_batch(self, batch):
        """Preprocesses a batch of images for YOLOE training, adjusting formatting and dimensions as needed."""
        batch = super(YOLOETrainer, self).preprocess_batch(batch)
        return batch


class YOLOEVPTrainer(YOLOETrainerFromScratch):
    """Train YOLOE model with visual prompts."""

    def preprocess_batch(self, batch):
        """Preprocesses a batch of images for YOLOE training, adjusting formatting and dimensions as needed."""
        batch = super().preprocess_batch(batch)
        batch["visuals"] = batch["visuals"].to(self.device)
        return batch
