# pylint: disable=no-member, too-many-locals, too-many-arguments, too-many-instance-attributes, invalid-name, attribute-defined-outside-init, no-name-in-module
"""Training scripts"""
from importlib import import_module
import gc
import os
import time
import warnings
from pprint import pprint
import math

import torch
from torch.utils.data import DataLoader
from torch import nn, randperm as rp
import numpy as np
import pandas as pd

from tqdm import tqdm
from apto.utils.report import get_classification_report

from omegaconf import OmegaConf, open_dict

import wandb

warnings.filterwarnings("ignore")


def trainer_factory(
    cfg, model_cfg, dataloaders, model, optimizer, scheduler, logger
):
    """Trainer factory"""
    if "custom_trainer" not in cfg.model or not cfg.model.custom_trainer:
        trainer = BasicTrainer(
            cfg,
            model_cfg,
            dataloaders,
            model,
            optimizer,
            scheduler,
            logger,
        )
    else:
        try:
            model_module = import_module(f"src.models.{cfg.model.name}")
        except ModuleNotFoundError as e:
            raise ModuleNotFoundError(
                f"No module named '{cfg.model.name}' \
                                    found in 'src.models'. Check if model name \
                                    in config file and its module name are the same"
            ) from e

        try:
            get_trainer = model_module.get_trainer
        except AttributeError as e:
            raise AttributeError(
                f"'src.models.{cfg.model.name}' has no function\
                                'get_trainer'. Is the function misnamed/not defined?"
            ) from e

        trainer = get_trainer(
            cfg,
            model_cfg,
            dataloaders,
            model,
            optimizer,
            scheduler,
            logger,
        )

    return trainer


class BasicTrainer:
    """Basic training script"""

    def __init__(
        self,
        cfg,
        model_cfg,
        dataloaders,
        model,
        optimizer,
        scheduler,
        logger,
    ) -> None:
        self.cfg = cfg
        self.model_cfg = model_cfg
        self.dataloaders = dataloaders
        self.model = model
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.logger = logger

        if "permute" in cfg and cfg.permute == "Multiple":
            self.permute = True
        else:
            self.permute = False

        params = self.count_params(self.model)
        self.logger.summary["params"] = params

        self.epochs = self.cfg.mode.max_epochs
        self.save_path = self.cfg.run_dir

        self.early_stopping = EarlyStopping(
            path=self.save_path,
            minimize=True,
            patience=self.cfg.mode.patience,
        )

        # set device
        if torch.cuda.is_available():
            # CUDA
            dev = "cuda:0"
        elif torch.backends.mps.is_available() and torch.backends.mps.is_built():
            # Apple Silicon's mps was too buggy, using cpu instead
            # dev = "mps"
            dev = "cpu"
        else:
            # CPU
            dev = "cpu"
        print(f"Used device: {dev}")
        with open_dict(self.cfg):
            self.cfg.device = dev
        self.device = torch.device(dev)

        self.model = model.to(self.device)

        # log configs
        self.logger.config.update(
            {"general": OmegaConf.to_container(self.cfg, resolve=True)}
        )
        self.logger.config.update(
            {"model": OmegaConf.to_container(self.model_cfg, resolve=True)}
        )

        # save configs in the run's directory
        with open(f"{self.save_path}/config.yaml", "w", encoding="utf8") as f:
            OmegaConf.save(self.cfg, f)
        with open(f"{self.save_path}/model_config.yaml", "w", encoding="utf8") as f:
            OmegaConf.save(self.model_cfg, f)

    def count_params(self, model, only_requires_grad: bool = False):
        "count number trainable parameters in a pytorch model"
        if only_requires_grad:
            total_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
        else:
            total_params = sum(p.numel() for p in model.parameters())
        return total_params

    def run_epoch(self, ds_name):
        """Run single epoch while monitoring for OutOfMemoryError"""
        impatience = 0
        while True:
            try:
                metrics = self.run_epoch_for_real(ds_name)
            except torch.cuda.OutOfMemoryError as e:
                if impatience > 5:
                    raise torch.cuda.OutOfMemoryError(
                        "Can't fix CUDA out of memory exception"
                    ) from e

                impatience += 1
                print("CUDA OOM encountered, reducing batch_size and cleaning memory")

                # run garbage collector and empty cache
                gc.collect()
                torch.cuda.empty_cache()

                # reduce batch_size
                with open_dict(self.cfg):
                    self.cfg.mode.batch_size //= 2
                for key in self.dataloaders:
                    dataset = self.dataloaders[key].dataset
                    self.dataloaders[key] = DataLoader(
                        dataset,
                        batch_size=self.cfg.mode.batch_size,
                        num_workers=0,
                        shuffle=key == "train",
                    )

                # try to run the epoch again
                continue

            # no errors encountered, exiting loop
            break

        return metrics

    def run_epoch_for_real(self, ds_name):
        """Run single epoch on `ds_name` dataloder"""
        is_train_dataset = ds_name == "train"

        all_scores, all_targets = [], []
        total_loss, total_size = 0.0, 0

        self.model.train(is_train_dataset)
        start_time = time.time()

        n_samples = len(self.dataloaders[ds_name].dataset)
        n_batches = math.ceil(n_samples / self.dataloaders[ds_name].batch_size)
        logs = []
        total_loss = 0
        with torch.set_grad_enabled(is_train_dataset):
            for data, target in self.dataloaders[ds_name]:
                # permute TS data if needed
                if is_train_dataset and self.permute:
                    for i, sample in enumerate(data):
                        data[i] = sample[rp(sample.shape[0]), :]

                # data, target = data.to(self.device), target.to(self.device)

                logits, loss_input = self.model(data)
                loss, log = self.model.loss(loss_input, target)
                score = torch.softmax(logits, dim=-1)
                if log is not None:
                    logs.append(log)

                all_scores.append(score.cpu().detach().numpy())
                all_targets.append(target.cpu().detach().numpy())
                total_loss += loss.item()

                if is_train_dataset:
                    self.optimizer.zero_grad()
                    loss.backward()
                    self.optimizer.step()

        average_time = (time.time() - start_time) / n_samples
        average_loss = total_loss / n_batches

        y_test = np.hstack(all_targets)
        y_score = np.vstack(all_scores)
        y_pred = np.argmax(y_score, axis=-1).astype(np.int32)

        report = get_classification_report(
            y_true=y_test, y_pred=y_pred, y_score=y_score, beta=0.5
        )

        metrics = {
            ds_name + "_accuracy": report["precision"].loc["accuracy"],
            ds_name + "_score": report["auc"].loc["weighted"],
            ds_name + "_average_loss": average_loss,
            ds_name + "_average_time": average_time,
        }
        if len(logs) != 0:
            log = pd.DataFrame(logs).mean().to_dict()
            for key, value in log.items():
                metrics[ds_name + '_' + key] = value

        return metrics

    def train(self):
        """Start training"""
        start_time = time.time()

        train_results = []
        for epoch in tqdm(range(self.epochs)):
            # run train and valid dataloaders
            results = self.run_epoch("train")
            results.update(self.run_epoch("valid"))

            # save results
            train_results.append(results)

            # update scheduler
            self.scheduler.step(results["valid_average_loss"])

            # check early stopping criterion
            self.early_stopping(results["valid_average_loss"], self.model, epoch)
            if self.early_stopping.early_stop:
                break

        if self.early_stopping.early_stop:
            print("EarlyStopping triggered")

        # log train results
        train_results = pd.DataFrame(train_results)
        train_results["epoch"] = train_results.index
        epoch = train_results.pop("epoch")
        train_results.insert(0, "epoch", epoch)
        train_results.to_csv(f"{self.save_path}/train_log.csv", index=False)

        table = wandb.Table(dataframe=train_results)
        self.logger.log(
            {
                "train_average_loss": wandb.plot.line(
                    table, "epoch", "train_average_loss", title="train_average_loss"
                )
            }
        )
        self.logger.log(
            {
                "valid_average_loss": wandb.plot.line(
                    table, "epoch", "valid_average_loss", title="valid_average_loss"
                )
            }
        )
        self.logger.log({"train_table": table})

        self.training_time = time.time() - start_time
        self.logger.summary["training_time"] = self.training_time

    def test(self):
        """Start testing"""
        for key in self.dataloaders:
            if key not in ["train", "valid"]:
                results = self.run_epoch(key)

                self.test_results.update(results)

        # log test results
        test_results = pd.DataFrame(self.test_results, index=[0])
        test_results.to_csv(f"{self.save_path}/test_log.csv", index=False)

        self.logger.log(self.test_results)

    def run(self):
        """Run training script"""

        print("Training model")
        self.train()

        print("Loading best model")
        model_logpath = f"{self.save_path}/best_model.pt"
        checkpoint = torch.load(
            model_logpath, map_location=lambda storage, loc: storage
        )
        self.model.load_state_dict(checkpoint)

        print("Testing trained model")
        self.test_results = {}
        self.test_results["training_time"] = self.training_time
        self.test()
        print("Test results:")
        pprint(self.test_results, indent=2)
        print("Done!")

        if not self.cfg.mode.preserve_checkpoints:
            os.remove(f"{self.save_path}/best_model.pt")

        return self.test_results


class EarlyStopping:
    """Early stops the training if the given score does not improve after a given patience."""

    def __init__(
        self,
        path: str,
        minimize: bool,
        patience: int = 30,
    ):
        assert minimize in [True, False]

        self.path = path
        self.minimize = minimize
        self.patience = patience
        self.counter = 0
        self.best_score = None
        self.early_stop = False

    def __call__(self, new_score, model, epoch):
        if self.best_score is None:
            self.best_score = new_score
            self.save_checkpoint(model)
        else:
            if self.minimize:
                change = self.best_score - new_score
            else:
                change = new_score - self.best_score

            if change > 0.0:
                self.counter = 0
                self.best_score = new_score
                self.save_checkpoint(model)
            else:
                self.counter += 1
                if self.counter >= self.patience:
                    self.early_stop = True

    def save_checkpoint(self, model):
        # based on callback from animus package
        """Saves model if criterion is met"""
        if isinstance(model, (nn.DataParallel, nn.parallel.DistributedDataParallel)):
            model = model.module

        if issubclass(model.__class__, torch.nn.Module):
            torch.save(model.state_dict(), f"{self.path}/best_model.pt")
        else:
            torch.save(model, f"{self.path}/best_model.pt")
