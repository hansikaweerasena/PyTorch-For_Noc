# -*- coding: utf-8 -*-
"""
Hyperparameter tuning with Ray Tune
===================================

Hyperparameter tuning can make the difference between an average model and a highly
accurate one. Often simple things like choosing a different learning rate or changing
a network layer size can have a dramatic impact on your model performance.

Fortunately, there are tools that help with finding the best combination of parameters.
`Ray Tune <https://docs.ray.io/en/latest/tune.html>`_ is an industry standard tool for
distributed hyperparameter tuning. Ray Tune includes the latest hyperparameter search
algorithms, integrates with TensorBoard and other analysis libraries, and natively
supports distributed training through `Ray's distributed machine learning engine
<https://ray.io/>`_.

In this tutorial, we will show you how to integrate Ray Tune into your PyTorch
training workflow. We will extend `this tutorial from the PyTorch documentation
<https://pytorch.org/tutorials/beginner/blitz/cifar10_tutorial.html>`_ for training
a CIFAR10 image classifier.

As you will see, we only need to add some slight modifications. In particular, we
need to

1. wrap data loading and training in functions,
2. make some network parameters configurable,
3. add checkpointing (optional),
4. and define the search space for the model tuning

|

To run this tutorial, please make sure the following packages are
installed:

-  ``ray[tune]``: Distributed hyperparameter tuning library
-  ``torchvision``: For the data transformers

Setup / Imports
---------------
Let's start with the imports:
"""
from functools import partial
import numpy as np
import os
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import random_split
from torch.utils.data import ConcatDataset
from torch.utils.data import Dataset, DataLoader
import torchvision
import torchvision.transforms as transforms
from ray import tune
from ray.tune import CLIReporter
from ray.tune.schedulers import ASHAScheduler
######################################################################
# Most of the imports are needed for building the PyTorch model. Only the last three
# imports are for Ray Tune.

# Data Set
# ------------
# Custom dataset class to support the data 


class MyDataset(Dataset):
    def __init__(self, data_dir, file_index):
        self.data_dir = data_dir
        self.file_index = file_index
        y = np.load(self.data_dir + "/Y/" + str(self.file_index) + ".npy", allow_pickle=True)
        self.y = torch.from_numpy(y)
    
    def __getitem__(self, index):
        x = np.load(self.data_dir + "/X/" + str(self.file_index) + ".npy", allow_pickle=True, mmap_mode='r')
        x = torch.from_numpy(x)
        return [x[index], self.y[index]]
    
    def __len__(self):
        return len(self.y)


# Data loaders
# ------------
# We wrap the data loaders in their own function and pass a global data directory.
# This way we can share a data directory between different trials.


def load_data(data_dir="./data"):
    list_of_dataset = []
    number_of_files = 41
    for i in range(number_of_files):
        list_of_dataset.append(MyDataset(data_dir,i))
    full_dataset = ConcatDataset(list_of_dataset)
    train_data_set, test_data_set = torch.utils.data.random_split(full_dataset, [10128, 6000]) 
    return train_data_set, test_data_set

######################################################################
# Configurable neural network
# ---------------------------
# We can only tune those parameters that are configurable. In this example, we can specify
# the layer sizes of the fully connected layers:


class Net(nn.Module):
    def __init__(self, W1=30, W2=10, K1=2000, K2=1000):
        super().__init__() 
        self.conv1 = nn.Conv2d(1, K1, (2,W1), stride=(2, 1))
        self.pool1 = nn.MaxPool2d((1, 5), stride=(1, 1))
        self.conv2 = nn.Conv2d(K1, K2, (1,W2), stride=(1, 1))
        self.pool2 = nn.MaxPool2d((1, 5), stride=(1, 1))

        # pass random data to identify the flattening shape
        x = torch.randn(2,450).view(-1,1,2,450)
        self._to_linear = None
        self.convs(x)
        
        self.fc1 = nn.Linear(self._to_linear, 3000)
        self.fc2 = nn.Linear(3000, 800) 
        self.fc3 = nn.Linear(800,100)
        self.fc4 = nn.Linear(100,2)

    def convs(self, x):
        x = self.pool1(F.relu(self.conv1(x)))
        x = self.pool2(F.relu(self.conv2(x)))

        if self._to_linear is None:
            self._to_linear = x[0].shape[0]*x[0].shape[1]*x[0].shape[2]
            print(self._to_linear)
        return x

    def forward(self, x):
        x = self.convs(x)  
        x = x.view(-1, self._to_linear)
        x = torch.flatten(x, start_dim=1)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = F.relu(self.fc3(x))
        x = self.fc4(x)
        return x


######################################################################
# The train function
# ------------------
# Now it gets interesting, because we introduce some changes to the example `from the PyTorch
# documentation <https://pytorch.org/tutorials/beginner/blitz/cifar10_tutorial.html>`_.
#
# We wrap the training script in a function ``train_cifar(config, checkpoint_dir=None, data_dir=None)``.
# As you can guess, the ``config`` parameter will receive the hyperparameters we would like to
# train with. The ``checkpoint_dir`` parameter is used to restore checkpoints. The ``data_dir`` specifies
# the directory where we load and store the data, so multiple runs can share the same data source.
#
# .. code-block:: python
#
#     net = Net(config["l1"], config["l2"])
#
#     if checkpoint_dir:
#         model_state, optimizer_state = torch.load(
#             os.path.join(checkpoint_dir, "checkpoint"))
#         net.load_state_dict(model_state)
#         optimizer.load_state_dict(optimizer_state)
#
# The learning rate of the optimizer is made configurable, too:
#
# .. code-block:: python
#
#     optimizer = optim.SGD(net.parameters(), lr=config["lr"], momentum=0.9)
#
# We also split the training data into a training and validation subset. We thus train on
# 80% of the data and calculate the validation loss on the remaining 20%. The batch sizes
# with which we iterate through the training and test sets are configurable as well.
#
# Adding (multi) GPU support with DataParallel
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Image classification benefits largely from GPUs. Luckily, we can continue to use
# PyTorch's abstractions in Ray Tune. Thus, we can wrap our model in ``nn.DataParallel``
# to support data parallel training on multiple GPUs:
#
# .. code-block:: python
#
#     device = "cpu"
#     if torch.cuda.is_available():
#         device = "cuda:0"
#         if torch.cuda.device_count() > 1:
#             net = nn.DataParallel(net)
#     net.to(device)
#
# By using a ``device`` variable we make sure that training also works when we have
# no GPUs available. PyTorch requires us to send our data to the GPU memory explicitly,
# like this:
#
# .. code-block:: python
#
#     for i, data in enumerate(trainloader, 0):
#         inputs, labels = data
#         inputs, labels = inputs.to(device), labels.to(device)
#
# The code now supports training on CPUs, on a single GPU, and on multiple GPUs. Notably, Ray
# also supports `fractional GPUs <https://docs.ray.io/en/master/using-ray-with-gpus.html#fractional-gpus>`_
# so we can share GPUs among trials, as long as the model still fits on the GPU memory. We'll come back
# to that later.
#
# Communicating with Ray Tune
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~
#
# The most interesting part is the communication with Ray Tune:
#
# .. code-block:: python
#
#     with tune.checkpoint_dir(epoch) as checkpoint_dir:
#         path = os.path.join(checkpoint_dir, "checkpoint")
#         torch.save((net.state_dict(), optimizer.state_dict()), path)
#
#     tune.report(loss=(val_loss / val_steps), accuracy=correct / total)
#
# Here we first save a checkpoint and then report some metrics back to Ray Tune. Specifically,
# we send the validation loss and accuracy back to Ray Tune. Ray Tune can then use these metrics
# to decide which hyperparameter configuration lead to the best results. These metrics
# can also be used to stop bad performing trials early in order to avoid wasting
# resources on those trials.
#
# The checkpoint saving is optional, however, it is necessary if we wanted to use advanced
# schedulers like
# `Population Based Training <https://docs.ray.io/en/master/tune/tutorials/tune-advanced-tutorial.html>`_.
# Also, by saving the checkpoint we can later load the trained models and validate them
# on a test set.
#
# Full training function
# ~~~~~~~~~~~~~~~~~~~~~~
#
# The full code example looks like this:


def train_net(config, checkpoint_dir=None, data_dir=None):
    net = Net(config["W1"], config["W2"], config["K1"], config["K2"])

    # device = "cpu"
    # if torch.cuda.is_available():
    #     device = "cuda:0"
    #     if torch.cuda.device_count() > 1:
    #         net = nn.DataParallel(net)
    # net.to(device)

    criterion = nn.CrossEntropyLoss()
    optimizer = optim.SGD(net.parameters(), lr=config["lr"], momentum=0.9)

    if checkpoint_dir:
        model_state, optimizer_state = torch.load(
            os.path.join(checkpoint_dir, "checkpoint"))
        net.load_state_dict(model_state)
        optimizer.load_state_dict(optimizer_state)

    trainset, testset = load_data(data_dir)

    test_abs = int(len(trainset) * 0.8)
    train_subset, val_subset = random_split(
        trainset, [test_abs, len(trainset) - test_abs])

    trainloader = torch.utils.data.DataLoader(
        train_subset,
        batch_size=int(config["batch_size"]),
        shuffle=True)
    valloader = torch.utils.data.DataLoader(
        val_subset,
        batch_size=int(config["batch_size"]),
        shuffle=True)

    for epoch in range(10):  # loop over the dataset multiple times
        running_loss = 0.0
        epoch_steps = 0
        for i, data in enumerate(trainloader, 0):
            # get the inputs; data is a list of [inputs, labels]
            inputs, labels = data
            # inputs, labels = inputs.to(device), labels.to(device)

            # zero the parameter gradients
            optimizer.zero_grad()

            inputs = inputs.type(torch.FloatTensor)
            # forward + backward + optimize
            outputs = net(inputs.view(-1,1,2,450))
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()

            # print statistics
            running_loss += loss.item()
            epoch_steps += 1
            if i % 2000 == 1999:  # print every 2000 mini-batches
                print("[%d, %5d] loss: %.3f" % (epoch + 1, i + 1,
                                                running_loss / epoch_steps))
                running_loss = 0.0

        # Validation loss
        val_loss = 0.0
        val_steps = 0
        total = 0
        correct = 0
        for i, data in enumerate(valloader, 0):
            with torch.no_grad():
                inputs, labels = data
                # inputs, labels = inputs.to(device), labels.to(device)

                inputs = inputs.type(torch.FloatTensor)

                outputs = net(inputs.view(-1,1,2,450))
                _, predicted = torch.max(outputs, 1)
                total += labels.size(0)
                correct += (predicted == labels).sum().item()

                loss = criterion(outputs, labels)
                val_loss += loss.cpu().numpy()
                val_steps += 1

        with tune.checkpoint_dir(epoch) as checkpoint_dir:
            path = os.path.join(checkpoint_dir, "checkpoint")
            torch.save((net.state_dict(), optimizer.state_dict()), path)

        tune.report(loss=(val_loss / val_steps), accuracy=correct / total)
    print("Finished Training")

######################################################################
# As you can see, most of the code is adapted directly from the original example.
#
# Test set accuracy
# -----------------
# Commonly the performance of a machine learning model is tested on a hold-out test
# set with data that has not been used for training the model. We also wrap this in a
# function:


def test_accuracy(net, device="cpu"):
    trainset, testset = load_data()

    testloader = torch.utils.data.DataLoader(
        testset, batch_size=4, shuffle=False)

    correct = 0
    total = 0
    with torch.no_grad():
        for data in testloader:
            images, labels = data
            # images, labels = images.to(device), labels.to(device)
            outputs = net(images)
            _, predicted = torch.max(outputs.data, 1)
            total += labels.size(0)
            correct += (predicted == labels).sum().item()

    return correct / total

######################################################################
# The function also expects a ``device`` parameter, so we can do the
# test set validation on a GPU.
#
# Configuring the search space
# ----------------------------
# Lastly, we need to define Ray Tune's search space. Here is an example:
#
# .. code-block:: python
#
#     config = {
#         "l1": tune.sample_from(lambda _: 2**np.random.randint(2, 9)),
#         "l2": tune.sample_from(lambda _: 2**np.random.randint(2, 9)),
#         "lr": tune.loguniform(1e-4, 1e-1),
#         "batch_size": tune.choice([2, 4, 8, 16])
#     }
#
# The ``tune.sample_from()`` function makes it possible to define your own sample
# methods to obtain hyperparameters. In this example, the ``l1`` and ``l2`` parameters
# should be powers of 2 between 4 and 256, so either 4, 8, 16, 32, 64, 128, or 256.
# The ``lr`` (learning rate) should be uniformly sampled between 0.0001 and 0.1. Lastly,
# the batch size is a choice between 2, 4, 8, and 16.
#
# At each trial, Ray Tune will now randomly sample a combination of parameters from these
# search spaces. It will then train a number of models in parallel and find the best
# performing one among these. We also use the ``ASHAScheduler`` which will terminate bad
# performing trials early.
#
# We wrap the ``train_cifar`` function with ``functools.partial`` to set the constant
# ``data_dir`` parameter. We can also tell Ray Tune what resources should be
# available for each trial:
#
# .. code-block:: python
#
#     gpus_per_trial = 2
#     # ...
#     result = tune.run(
#         partial(train_cifar, data_dir=data_dir),
#         resources_per_trial={"cpu": 8, "gpu": gpus_per_trial},
#         config=config,
#         num_samples=num_samples,
#         scheduler=scheduler,
#         progress_reporter=reporter,
#         checkpoint_at_end=True)
#
# You can specify the number of CPUs, which are then available e.g.
# to increase the ``num_workers`` of the PyTorch ``DataLoader`` instances. The selected
# number of GPUs are made visible to PyTorch in each trial. Trials do not have access to
# GPUs that haven't been requested for them - so you don't have to care about two trials
# using the same set of resources.
#
# Here we can also specify fractional GPUs, so something like ``gpus_per_trial=0.5`` is
# completely valid. The trials will then share GPUs among each other.
# You just have to make sure that the models still fit in the GPU memory.
#
# After training the models, we will find the best performing one and load the trained
# network from the checkpoint file. We then obtain the test set accuracy and report
# everything by printing.
#
# The full main function looks like this:


def main(num_samples=10, max_num_epochs=10, gpus_per_trial=2, cpus_per_trial=2):
    data_dir = os.path.abspath("/export/research26/cyclone/hansika/64_nodes_100")
    checkpoint_dir = os.path.abspath("/export/research26/cyclone/hansika/cheakpoint")
    load_data(data_dir)
    config = {
        "W1": tune.choice([5, 10, 20, 30]),
        "W2": tune.choice([5, 10, 20, 30]),
        "K1": tune.choice([500, 1000, 2000, 3000]),
        "K2": tune.choice([500, 1000, 2000, 3000]),
        "lr": tune.loguniform(1e-4, 1e-1),
        "batch_size": tune.choice([5, 10, 20, 50])
    }
    scheduler = ASHAScheduler(
        metric="loss",
        mode="min",
        max_t=max_num_epochs,
        grace_period=1,
        reduction_factor=2)
    reporter = CLIReporter(
        # parameter_columns=["l1", "l2", "lr", "batch_size"],
        metric_columns=["loss", "accuracy", "training_iteration"])
    result = tune.run(
        partial(train_net, data_dir=data_dir, checkpoint_dir=checkpoint_dir),
        local_dir="/export/research26/cyclone/hansika/ray_results",
        resources_per_trial={"cpu": cpus_per_trial, "gpu": gpus_per_trial},
        config=config,
        num_samples=num_samples,
        scheduler=scheduler,
        progress_reporter=reporter)

    best_trial = result.get_best_trial("loss", "min", "last")
    print("Best trial config: {}".format(best_trial.config))
    print("Best trial final validation loss: {}".format(
        best_trial.last_result["loss"]))
    print("Best trial final validation accuracy: {}".format(
        best_trial.last_result["accuracy"]))

    best_trained_model = Net(best_trial.config["W1"], best_trial.config["W2"], best_trial.config["K1"], best_trial.config["K2"])
    # device = "cpu"
    # if torch.cuda.is_available():
    #     device = "cuda:0"
    #     if gpus_per_trial > 1:
    #         best_trained_model = nn.DataParallel(best_trained_model)
    # best_trained_model.to(device)

    best_checkpoint_dir = best_trial.checkpoint.value
    model_state, optimizer_state = torch.load(os.path.join(
        best_checkpoint_dir, "checkpoint"))
    best_trained_model.load_state_dict(model_state)

    test_acc = test_accuracy(best_trained_model, device)
    print("Best trial test set accuracy: {}".format(test_acc))


if __name__ == "__main__":
    # You can change the number of GPUs per trial here:
    main(num_samples=2, max_num_epochs=10, gpus_per_trial=0, cpus_per_trial=41)


######################################################################
# If you run the code, an example output could look like this:
#
# ::
#
#     Number of trials: 10 (10 TERMINATED)
#     +-----+------+------+-------------+--------------+---------+------------+--------------------+
#     | ... |   l1 |   l2 |          lr |   batch_size |    loss |   accuracy | training_iteration |
#     |-----+------+------+-------------+--------------+---------+------------+--------------------|
#     | ... |   64 |    4 | 0.00011629  |            2 | 1.87273 |     0.244  |                  2 |
#     | ... |   32 |   64 | 0.000339763 |            8 | 1.23603 |     0.567  |                  8 |
#     | ... |    8 |   16 | 0.00276249  |           16 | 1.1815  |     0.5836 |                 10 |
#     | ... |    4 |   64 | 0.000648721 |            4 | 1.31131 |     0.5224 |                  8 |
#     | ... |   32 |   16 | 0.000340753 |            8 | 1.26454 |     0.5444 |                  8 |
#     | ... |    8 |    4 | 0.000699775 |            8 | 1.99594 |     0.1983 |                  2 |
#     | ... |  256 |    8 | 0.0839654   |           16 | 2.3119  |     0.0993 |                  1 |
#     | ... |   16 |  128 | 0.0758154   |           16 | 2.33575 |     0.1327 |                  1 |
#     | ... |   16 |    8 | 0.0763312   |           16 | 2.31129 |     0.1042 |                  4 |
#     | ... |  128 |   16 | 0.000124903 |            4 | 2.26917 |     0.1945 |                  1 |
#     +-----+------+------+-------------+--------------+---------+------------+--------------------+
#
#
#     Best trial config: {'l1': 8, 'l2': 16, 'lr': 0.00276249, 'batch_size': 16, 'data_dir': '...'}
#     Best trial final validation loss: 1.181501
#     Best trial final validation accuracy: 0.5836
#     Best trial test set accuracy: 0.5806
#
# Most trials have been stopped early in order to avoid wasting resources.
# The best performing trial achieved a validation accuracy of about 58%, which could
# be confirmed on the test set.
#
# So that's it! You can now tune the parameters of your PyTorch models.
