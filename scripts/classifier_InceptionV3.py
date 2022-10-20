#==============================================================================#
#  Author:       Dominik Müller                                                #
#  Copyright:    2021 IT-Infrastructure for Translational Medical Research,    #
#                University of Augsburg                                        #
#                                                                              #
#  This program is free software: you can redistribute it and/or modify        #
#  it under the terms of the GNU General Public License as published by        #
#  the Free Software Foundation, either version 3 of the License, or           #
#  (at your option) any later version.                                         #
#                                                                              #
#  This program is distributed in the hope that it will be useful,             #
#  but WITHOUT ANY WARRANTY; without even the implied warranty of              #
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the               #
#  GNU General Public License for more details.                                #
#                                                                              #
#  You should have received a copy of the GNU General Public License           #
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.       #
#==============================================================================#
#-----------------------------------------------------#
#                   Library imports                   #
#-----------------------------------------------------#
# External libraries
import os
import json
import pandas as pd
import numpy as np
from tensorflow.keras.callbacks import ModelCheckpoint, CSVLogger, \
                                       ReduceLROnPlateau
from tensorflow.keras.metrics import AUC
# AUCMEDI libraries
from aucmedi import input_interface, DataGenerator, Neural_Network, Image_Augmentation
from aucmedi.neural_network.architectures import supported_standardize_mode
from aucmedi.utils.class_weights import compute_multilabel_weights
from aucmedi.data_processing.subfunctions import Padding
from aucmedi.sampling import sampling_kfold
from aucmedi.neural_network.architectures import architecture_dict
from aucmedi.utils.callbacks import MinEpochEarlyStopping
from aucmedi.neural_network.loss_functions import multilabel_focal_loss
# Custom libraries
from retinal_crop import Retinal_Crop

#-----------------------------------------------------#
#                   Configurations                    #
#-----------------------------------------------------#
os.environ["CUDA_VISIBLE_DEVICES"]="2"

# Provide pathes to imaging and annotation data
# path_riadd = "/storage/riadd2021/Training_Set/"
path_riadd = "/storage/riadd2021/Upsampled_Set/"

# Define some parameters
k_fold = 5
processes = 8
batch_queue_size = 16
threads = 16

# Define architecture which should be processed
arch = "InceptionV3"

# Define input shape
input_shape = (224, 224)

#-----------------------------------------------------#
#          AUCMEDI Classifier Setup for RIADD         #
#-----------------------------------------------------#
# path_images = os.path.join(path_riadd, "Training")
path_images = os.path.join(path_riadd, "images")
# path_csv = os.path.join(path_riadd, "RFMiD_Training_Labels.csv")
path_csv = os.path.join(path_riadd, "data.csv")

# Initialize input data reader
cols = ["DR", "ARMD", "MH", "DN", "MYA", "BRVO", "TSLN", "ERM", "LS", "MS",
        "CSR", "ODC", "CRVO", "TV", "AH", "ODP", "ODE", "ST", "AION", "PT",
        "RT", "RS", "CRS", "EDN", "RPEC", "MHL", "RP", "OTHER"]
ds = input_interface(interface="csv", path_imagedir=path_images,
                     path_data=path_csv, ohe=True, col_sample="ID",
                     ohe_range=cols)
(index_list, class_ohe, nclasses, class_names, image_format) = ds

# Create models directory
path_models = os.path.join("models")
if not os.path.exists(path_models) : os.mkdir(path_models)

# Sample dataset via k-fold cross-validation
if os.path.exists(os.path.join(path_models, "sampling.json")):
    # Load sampling from disk
    with open(os.path.join(path_models, "sampling.json"), "r") as json_reader:
        sampling_dict = json.load(json_reader)
    subsets = []
    for i in range(0, k_fold):
        fold = "cv_" + str(i)
        x_train = np.array(sampling_dict[fold]["x_train"])
        y_train = np.array(sampling_dict[fold]["y_train"])
        x_val = np.array(sampling_dict[fold]["x_val"])
        y_val = np.array(sampling_dict[fold]["y_val"])
        subsets.append((x_train, y_train, x_val, y_val))
else:
    # Perform sampling
    subsets = sampling_kfold(index_list, class_ohe, n_splits=k_fold,
                             stratified=True, iterative=True, seed=0)

    # Store sampling to disk
    sampling_dict = {}
    for i, fold in enumerate(subsets):
        (x_train, y_train, x_val, y_val) = fold
        sampling_dict["cv_" + str(i)] = {"x_train": x_train.tolist(),
                                         "y_train": y_train.tolist(),
                                         "x_val": x_val.tolist(),
                                         "y_val": y_val.tolist()}
    with open(os.path.join(path_models, "sampling.json"), "w") as file:
        json.dump(sampling_dict, file, indent=2)

# Initialize Image Augmentation
aug = Image_Augmentation(flip=True, rotate=True, brightness=True, contrast=True,
                         saturation=True, hue=True, scale=False, crop=False,
                         grid_distortion=False, compression=False, gamma=False,
                         gaussian_noise=False, gaussian_blur=False,
                         downscaling=False, elastic_transform=False)
# Define Subfunctions
sf_list = [Padding(mode="square"), Retinal_Crop()]
# Set activation output to sigmoid for multi-label classification
activation_output = "sigmoid"

#-----------------------------------------------------#
#        AUCMEDI Classifier Training for RIADD        #
#-----------------------------------------------------#
# Create architecture directory
path_arch = os.path.join(path_models, "classifier_" + arch)
if not os.path.exists(path_arch) : os.mkdir(path_arch)

# Iterate over each fold of the CV
for i, fold in enumerate(subsets):
    # Obtain data samplings
    (x_train, y_train, x_val, y_val) = fold

    # Compute class weights
    class_weights = compute_multilabel_weights(ohe_array=y_train)

    # Initialize architecture
    nn_arch = architecture_dict[arch](channels=3, input_shape=input_shape)

    # Initialize model
    model = Neural_Network(nclasses, channels=3, architecture=nn_arch,
                           workers=processes,
                           batch_queue_size=batch_queue_size,
                           activation_output=activation_output,
                           loss=multilabel_focal_loss(class_weights),
                           metrics=["binary_accuracy", AUC(100)],
                           pretrained_weights=True, multiprocessing=True)
    # Modify number of transfer learning epochs with frozen model layers
    model.tf_epochs = 10

    # Obtain standardization mode for current architecture
    sf_standardize = supported_standardize_mode[arch]

    # Initialize training and validation Data Generators
    train_gen = DataGenerator(x_train, path_images, labels=y_train,
                              batch_size=48, img_aug=aug, shuffle=True,
                              subfunctions=sf_list, resize=input_shape,
                              standardize_mode=sf_standardize,
                              grayscale=False, prepare_images=False,
                              sample_weights=None, seed=None,
                              image_format=image_format, workers=threads)
    val_gen = DataGenerator(x_val, path_images, labels=y_val, batch_size=48,
                            img_aug=None, subfunctions=sf_list, shuffle=False,
                            standardize_mode=sf_standardize, resize=input_shape,
                            grayscale=False, prepare_images=False, seed=None,
                            sample_weights=None,
                            image_format=image_format, workers=threads)

    # Define callbacks
    cb_mc = ModelCheckpoint(os.path.join(path_arch, "cv_" + str(i) + \
                                         ".model.best.hdf5"),
                            monitor="val_loss", verbose=1,
                            save_best_only=True, mode="min")
    cb_cl = CSVLogger(os.path.join(path_arch, "cv_" + str(i) + ".logs.csv"),
                      separator=',', append=True)
    cb_lr = ReduceLROnPlateau(monitor='val_loss', factor=0.1, patience=8,
                              verbose=1, mode='min', min_lr=1e-7)
    cb_es = MinEpochEarlyStopping(monitor='val_loss', patience=20, verbose=1,
                                  start_epoch=60)
    callbacks = [cb_mc, cb_cl, cb_lr, cb_es]

    # Train model
    model.train(train_gen, val_gen, epochs=300, iterations=250,
                callbacks=callbacks, transfer_learning=True)

    # Dump latest model
    model.dump(os.path.join(path_arch, "cv_" + str(i) + ".model.last.hdf5"))

    # Garbage collection
    del train_gen
    del val_gen
    del model
