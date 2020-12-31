import os
from collections import deque
from typing import Any, Dict, List, Optional
from pathlib import Path

import hydra
import pytorch_lightning as pl
import wandb
from omegaconf import OmegaConf, DictConfig

from seg_lapa import metrics
from seg_lapa.config_parse import train_conf
from seg_lapa.config_parse.train_conf import TrainConf
from seg_lapa.loss_func import CrossEntropy2D
from seg_lapa.utils.path_check import get_project_root
from seg_lapa.callbacks import Mode, LogMedia

LOGS_DIR = "logs"


def is_rank_zero():
    local_rank = int(os.environ.get("LOCAL_RANK", 0))
    node_rank = int(os.environ.get("NODE_RANK", 0))
    if local_rank == 0 and node_rank == 0:
        return True

    return False


def generate_run_id(cfg: DictConfig):
    # Set the run ID: Read from config if resuming training, else generate unique id
    # TODO: read from cfg if resuming training - get from config dataclass! add method to resume training section.
    run_id = wandb.util.generate_id()
    return run_id


def create_log_dir(cfg: DictConfig, run_id: str) -> Optional[Path]:
    """Each run's log dir will have same name as wandb runid"""
    log_root_dir = get_project_root() / LOGS_DIR

    if is_rank_zero():
        log_dir = log_root_dir / run_id
        log_dir.mkdir(parents=True, exist_ok=True)

        # Save the input config file to logs dir
        OmegaConf.save(cfg, log_dir / "train.yaml")
    else:
        return log_root_dir / "None"

    return log_dir


def fix_seeds(random_seed: Optional[int]) -> None:
    """Fix seeds for reproducibility.
    Ref:
        https://pytorch.org/docs/stable/notes/randomness.html

    Args:
        random_seed: If None, seeds not set. If int, uses value to seed.
    """
    if random_seed is not None:
        pl.seed_everything(random_seed)


class DeeplabV3plus(pl.LightningModule):
    def __init__(self, config: TrainConf, log_media_max_batches=1):
        super().__init__()
        self.cross_entropy_loss = CrossEntropy2D(loss_per_image=True, ignore_index=255)
        self.config = config
        self.model = self.config.model.get_model()

        self.iou_train = metrics.Iou(num_classes=config.model.num_classes)
        self.iou_val = metrics.Iou(num_classes=config.model.num_classes)
        self.iou_test = metrics.Iou(num_classes=config.model.num_classes)

        # Returning images from _step methods is memory-expensive. Save predictions to be logged in a circular queue
        # and consume in a callback.
        self.log_media: Dict[Mode, deque] = LogMedia.get_empty_data_queue(log_media_max_batches)

    def forward(self, x):
        """In lightning, forward defines the prediction/inference actions.
        This method can be called elsewhere in the LightningModule with: `outputs = self(inputs)`.
        """
        outputs = self.model(x)
        return outputs

    def training_step(self, batch, batch_idx):
        """Defines the train loop. It is independent of forward().
        Don’t use any cuda or .to(device) calls in the code. PL will move the tensors to the correct device.
        """
        inputs, labels = batch
        outputs = self.model(inputs)
        predictions = outputs.argmax(dim=1)

        # Calculate Loss
        loss = self.cross_entropy_loss(outputs, labels)

        """Log the value on GPU0 per step. Also log average of all steps at epoch_end."""
        # self.log("Train/loss", loss, on_step=True, on_epoch=True)
        """Log the avg. value across all GPUs per step. Also log average of all steps at epoch_end.
        Alternately, you can use the ops 'sum' or 'avg'.
        Using sync_dist is efficient. It adds extremely minor overhead for scalar values.
        """
        self.log("Train/loss", loss, on_step=True, on_epoch=True, sync_dist=True, sync_dist_op="avg")

        # Calculate Metrics
        self.iou_train(predictions, labels)

        # Returning images is expensive - All the batches are accumulated for _epoch_end().
        # Save the latst predictions to be logged in an attr. They will be consumed by the LogMedia callback.
        self.log_media[Mode.TRAIN].append({"inputs": inputs, "labels": labels, "preds": predictions})

        return {"loss": loss}

    def validation_step(self, batch, batch_idx):
        inputs, labels = batch
        outputs = self.model(inputs)
        predictions = outputs.argmax(dim=1)

        # Calculate Loss
        loss = self.cross_entropy_loss(outputs, labels)
        self.log("Val/loss", loss, sync_dist=True, sync_dist_op="avg")

        # Calculate Metrics
        self.iou_val(predictions, labels)

        # Save the latest predictions to be logged
        self.log_media[Mode.VAL].append({"inputs": inputs, "labels": labels, "preds": predictions})

        return {"val_loss": loss}

    def test_step(self, batch, batch_idx):
        inputs, labels = batch
        outputs = self.model(inputs)
        predictions = outputs.argmax(dim=1)

        # Calculate Loss
        loss = self.cross_entropy_loss(outputs, labels)
        self.log("Test/loss", loss, sync_dist=True, sync_dist_op="avg")

        # Calculate Metrics
        self.iou_test(predictions, labels)

        # Save the latest predictions to be logged
        self.log_media[Mode.TEST].append({"inputs": inputs, "labels": labels, "preds": predictions})

        return {"test_loss": loss}

    def training_epoch_end(self, outputs: List[Any]):
        # Compute and log metrics across epoch
        metrics_avg = self.iou_train.compute()
        self.log("Train/mIoU", metrics_avg.miou)
        self.iou_train.reset()

    def validation_epoch_end(self, outputs: List[Any]):
        # Compute and log metrics across epoch
        metrics_avg = self.iou_val.compute()
        self.log("Val/mIoU", metrics_avg.miou)
        self.iou_val.reset()

    def test_epoch_end(self, outputs: List[Any]):
        # Compute and log metrics across epoch
        metrics_avg = self.iou_test.compute()
        self.log("Test/mIoU", metrics_avg.miou)
        self.iou_test.reset()

    def configure_optimizers(self):
        optimizer = self.config.optimizer.get_optimizer(self.parameters())
        return optimizer


@hydra.main(config_path="config", config_name="train")
def main(cfg: DictConfig):
    # if is_rank_zero():
    #     print("\nGiven Config:\n", OmegaConf.to_yaml(cfg))

    config = train_conf.parse_config(cfg)
    if is_rank_zero():
        print("\nResolved Dataclass:\n", config, "\n")

    fix_seeds(config.random_seed)
    run_id = generate_run_id(cfg)
    log_dir = create_log_dir(cfg, run_id)

    wb_logger = config.logger.get_logger(cfg, run_id, get_project_root())
    callbacks = config.callbacks.get_callbacks_list(log_dir)
    trainer = config.trainer.get_trainer(wb_logger, callbacks, get_project_root())
    model = DeeplabV3plus(config)
    dm = config.dataset.get_datamodule()

    # Run Training
    trainer.fit(model, datamodule=dm)

    # Run Testing
    result = trainer.test(ckpt_path=None)  # Prints the final result

    wandb.finish()


if __name__ == "__main__":
    main()
