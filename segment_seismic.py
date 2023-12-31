import torch
import json
from argparse import ArgumentParser
from datetime import datetime
from tqdm import tqdm
from math import ceil
from sklearn.model_selection import KFold

import core
from core.loader.data_loader import *
from core.models import load_empty_model
from core.metrics import RunningMetrics, EarlyStopper


def train_test_split(args: ArgumentParser, dataset: SeismicDataset) -> list:
    if args.cross_validation:
        kf = KFold(n_splits=5, shuffle=False)

        if args.few_shot:
            # 20% train / 80% test
            splits = [
                (test_idx.tolist(), train_idx.tolist()) for train_idx, test_idx in kf.split(dataset)
            ]
        else:
            # 80% train / 20% test
            splits = [
                (train_idx.tolist(), test_idx.tolist()) for train_idx, test_idx in kf.split(dataset)
            ]
    else:
        if args.few_shot:
            # 20% train / 80% test
            test_ratio = 0.8
        else:
            # 80% train / 20% test
            test_ratio = 0.2

        test_size  = int(len(dataset) * test_ratio)
        splits = [(list(range(test_size, len(dataset))), list(range(0, test_size)))]

    return splits


def store_results(args: ArgumentParser, results: dict) -> None:
    # Creating the results folder if it does not exist
    if not os.path.exists(args.results_path):
        os.makedirs(args.results_path)

    results_folder = os.path.join(args.results_path, datetime.now().isoformat('#'))
    os.makedirs(results_folder)

    # Storing metadata
    with open(os.path.join(results_folder, f'metadata.json'), 'w') as json_buffer:
        json.dump(vars(args), json_buffer, indent=4)

    for fold_number in sorted(results.keys()):
        suffix = f'_fold_{fold_number + 1}' if args.cross_validation else ''

        model  = results[fold_number]['model']
        scores = {key: value for key, value in results[fold_number].items() if key != 'model'}

        # Storing model weights
        torch.save(model.state_dict(), os.path.join(results_folder, 'model' + suffix + '.pt'))

        # Storing metric scores
        with open(os.path.join(results_folder, 'scores' + suffix + '.json'), 'w') as json_buffer:
            json.dump(scores, json_buffer, indent=4)
    
    print(f'\nModel and scores saved in {args.results_path}')


def run(args: ArgumentParser) -> dict:
    if args.data_path.endswith('/'):
        args.data_path = args.data_path[:-1]

    if not os.path.isdir(args.data_path):
        raise FileNotFoundError(f'Folder {args.data_path} does not exist.')
    
    print('')
    print('Data path:    ', args.data_path)
    print('Results path: ', args.results_path)
    print('')

    device = torch.device(args.device if torch.cuda.is_available() else 'cpu')

    print('Weighted loss ENABLED' if args.weighted_loss else 'Weighted loss DISABLED')
    print('Loading dataset...\n')

    dataset = SeismicDataset(
        data_path=args.data_path,
        orientation=args.orientation,
        compute_weights=args.weighted_loss
    )

    if args.weighted_loss:
        class_weights = torch.tensor(dataset.get_class_weights(), device=device, requires_grad=False)
        class_weights = class_weights.float()
    else:
        class_weights = None

    loss_map = {
        'cel': ('CrossEntropyLoss', {'reduction': 'sum', 'weight': class_weights})
    }

    # Defining the loss function
    loss_name, loss_args = loss_map[args.loss_function]
    criterion = getattr(core.loss, loss_name)(**loss_args)

    print(f'Training with {"INLINES" if args.orientation == "in" else "CROSSLINES"}')

    # Splitting the data into train and test
    splits  = train_test_split(args, dataset)
    results = {}

    for fold_number, (train_indices, test_indices) in enumerate(splits):
        if args.cross_validation:
            print(f'\n======== FOLD {fold_number + 1}/5 ========')

        train_loader = torch.utils.data.DataLoader(
            dataset=dataset,
            batch_size=args.batch_size,
            sampler=torch.utils.data.SubsetRandomSampler(train_indices),
        )
        test_loader = torch.utils.data.DataLoader(
            dataset=dataset,
            batch_size=args.batch_size,
            sampler=torch.utils.data.SubsetRandomSampler(test_indices),
        )

        print('\nCreating model...')
        print('Architecture:   ', args.architecture.upper())
        print('Optimizer:      ', args.optimizer)
        print('Loss function:  ', args.loss_function)
        print('Learning rate:  ', args.learning_rate)
        print('Device:         ', device)
        print('Batch size:     ', args.batch_size)
        print('N. of epochs:   ', args.n_epochs)

        print('')
        print('Number of training examples:', len(train_indices))
        print('Number of test examples:    ', len(test_indices))
        
        model = load_empty_model(args.architecture, dataset.get_n_classes())
        model = model.to(device)

        # Defining the optimizer
        optimizer_map = {
            'adam': torch.optim.Adam,
            'sgd' : torch.optim.SGD
        }

        OptimizerClass = optimizer_map[args.optimizer]
        optimizer      = OptimizerClass(
            params=model.parameters(),
            lr=args.learning_rate,
            weight_decay=args.weight_decay
        )

        # Initializing metrics
        train_metrics = RunningMetrics(n_classes=dataset.get_n_classes(), bf1_threshold=2)
        test_metrics  = RunningMetrics(n_classes=dataset.get_n_classes(), bf1_threshold=2)

        early_stopper = EarlyStopper(patience=3, min_delta=10)

        # Storing the loss for each epoch
        train_loss_list = []
        test_loss_list  = []

        for epoch in range(args.n_epochs):
            # Training phase
            model.train()
            train_loss = 0

            print(datetime.now().strftime('\n%Y/%m/%d %H:%M:%S'))
            print(f'[FOLD {fold_number + 1}] Training on epoch {epoch + 1}/{args.n_epochs}\n')

            for images, labels in tqdm(train_loader, ascii=' >='):
                optimizer.zero_grad()

                images = images.type(torch.FloatTensor).to(device)
                labels = labels.type(torch.FloatTensor).to(device)

                outputs = model(images)

                # Updating the running metrics
                train_metrics.update(images=outputs, targets=labels)

                # Computing the loss and updating weights
                loss = criterion(images=outputs, targets=labels.long())
                train_loss += loss.item()
                loss.backward()

                optimizer.step()
            
            train_loss = train_loss / ceil((len(train_loader) / args.batch_size))
            train_scores = train_metrics.get_scores()

            print(f'Train loss: {train_loss}')
            print(f'Train mIoU: {train_scores["mean_iou"]}')

            train_loss_list.append(train_loss)
            train_metrics.reset()
        
        # Testing phase
        with torch.no_grad():
            model.eval()
            test_loss = 0

            print(datetime.now().strftime('\n%Y/%m/%d %H:%M:%S'))
            print('Testing the model...\n')

            for images, labels in tqdm(test_loader, ascii=' >='):
                images = images.type(torch.FloatTensor).to(device)
                labels = labels.type(torch.FloatTensor).to(device)

                outputs = model(images)

                # Updating the running metrics
                test_metrics.update(images=outputs, targets=labels)

                # Computing the loss
                loss = criterion(images=outputs, targets=labels.long())
                test_loss += loss.item()
            
            test_loss = test_loss / ceil((len(test_loader) / args.batch_size))
            test_scores = test_metrics.get_scores()

            print(f'Test loss: {test_loss}')
            print(f'Test mIoU: {test_scores["mean_iou"]}')

            test_loss_list.append(test_loss)
            test_metrics.reset()
            
        # if early_stopper.early_stop(test_loss):             
        #     break
        
        results[fold_number] = {
            'model'        : model,
            'train_scores' : train_scores,
            'test_scores'  : test_scores,
            'train_losses' : train_loss_list,
            'test_losses'  : test_loss_list,
            'train_indices': train_indices,
            'test_indices' : test_indices
        }
    
    return results


if __name__ == '__main__':
    parser = ArgumentParser(description='Hyperparameters')

    parser.add_argument('-a', '--architecture',
        dest='architecture',
        type=str,
        help='Architecture to use [segnet, unet, deconvnet]',
        choices=['segnet', 'unet', 'deconvnet']
    )
    parser.add_argument('-p', '--data-path',
        dest='data_path',
        type=str,
        help='Path to the folder containing the dataset and its labels in .npy format'
    )
    parser.add_argument('-b', '--batch-size',
        dest='batch_size',
        type=int,
        default=16,
        help='Batch Size'
    )
    parser.add_argument('-d', '--device',
        dest='device',
        type=str,
        default='cuda:0',
        help='Device to train on [cuda:n]'
    )
    parser.add_argument('-v', '--cross-validation',
        dest='cross_validation',
        action='store_true',
        default=False,
        help='Whether to use 5-fold cross validation'
    )
    parser.add_argument('-L', '--loss-function',
        dest='loss_function',
        type=str,
        default='cel',
        help='Loss function to use [cel (Cross_Entropy Loss)]',
        choices=['cel']
    )
    parser.add_argument('-o', '--optimizer',
        dest='optimizer',
        type=str,
        default='adam',
        help='Optimizer to use [adam, sgd (Stochastic Gradient Descent)]',
        choices=['adam', 'sgd']
    )
    parser.add_argument('-l', '--learning-rate',
        dest='learning_rate',
        type=float,
        default=1e-4,
        help='Learning rate'
    )
    parser.add_argument('-w', '--weight-decay',
        dest='weight_decay',
        type=float,
        default=1e-5,
        help='L2 regularization. Value 0 indicates no weight decay'
    )
    parser.add_argument('-W', '--weighted-loss',
        dest='weighted_loss',
        action='store_true',
        default=False,
        help='Whether to use class weights in the loss function'
    )
    parser.add_argument('-e', '--n-epochs',
        dest='n_epochs',
        type=int,
        default=20,
        help='Number of epochs'
    )
    parser.add_argument('-O', '--orientation',
        dest='orientation',
        type=str,
        default='in',
        help='Whether the model should be trained using inlines or crosslines',
        choices=['in', 'cross']
    )
    parser.add_argument('-f', '--remove-faulty-slices',
        dest='remove_faulty_slices',
        action='store_true',
        default=True,
        help='Whether to remove slices with artifacts'
    )
    parser.add_argument('-F', '--few-shot',
        dest='few_shot',
        action='store_true',
        default=False,
        help='Whether to swap the train and test sets to train on less data'
    )
    parser.add_argument('-s', '--store-results',
        dest='store_results',
        action='store_true',
        default=True,
        help='Whether to store the model weights and metrics'
    )
    parser.add_argument('-r', '--results-path',
        dest='results_path',
        type=str,
        default=os.path.join(os.getcwd(), 'results'),
        help='Directory for storing execution results'
    )

    args = parser.parse_args(args=None)
    results = run(args)

    if args.store_results:
        store_results(args, results)

    # # Loading a model if it was previously stored
    # if args.stored_model_path:
    #     if os.path.isfile(args.stored_model_path):
    #         print(f'Loading stored model {args.stored_model_path}')

    #         model = load_empty_model(args.architecture, 1, dataset.n_classes)
    #         model.load_state_dict(torch.load(args.stored_model_path))
    #     else:
    #         raise FileNotFoundError(f'No such file or directory for stored model: {args.stored_model_path}')
    # else:
    #     model = train(train_set, device=device, criterion=criterion, args=args)

    # test(test_set, model=model, device=device, criterion=criterion, args=args)
