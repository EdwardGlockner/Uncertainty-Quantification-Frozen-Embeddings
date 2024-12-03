import os

from os.path import join as ospj
from os.path import expanduser
from munch import Munch as mch
from tqdm import tqdm_notebook
import numpy as np
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
import utils

import clip
import ds 
from ds import prepare_coco_dataloaders
from tqdm import tqdm
from losses import *
from utils import *

from ds.vocab import Vocabulary

from networks import *
from networks_mc_do import *
from networks_BBB_EncBL import *

def get_recall_at_1(pred_ranks, ground_truth_indices):
    """
    Args:
        pred_ranks (np.ndarray, shape=[#queries, 1]):
            Predicted top-1 gallery index for each query.
        ground_truth_indices (list[int]):
            Ground-truth gallery indices corresponding to each query.
    Returns:
        recall_1 (float): Recall@1 score.
    """
    # Flatten pred_ranks since it's shape [#queries, 1]
    pred_ranks = pred_ranks.flatten()
    
    # Calculate recall as the proportion of correct predictions
    correct_predictions = sum(pred == gt for pred, gt in zip(pred_ranks, ground_truth_indices))
    recall_1 = correct_predictions / len(ground_truth_indices)

    return recall_1


def eval_ProbVLM(
    CLIP_Net,
    BayesCap_Net,
    eval_loader,
    device='cuda',
    dtype=torch.cuda.FloatTensor,
):
    CLIP_Net.to(device)
    CLIP_Net.eval()
    BayesCap_Net.to(device)
    BayesCap_Net.eval()

    mean_mse = 0
    mean_mae = 0
    num_imgs = 0
    list_error = []
    list_var = []
    with tqdm(eval_loader, unit='batch') as tepoch:
        for (idx, batch) in enumerate(tepoch):
            tepoch.set_description('Validating ...')
            ##
            xI, xT  = batch[0].to(device), batch[1].to(device)
            # xI, xT = xI.type(dtype), xT.type(dtype)
            
            # pass them through the network
            with torch.no_grad():
                xfI, xfT = CLIP_Net(xI, xT)
                (img_mu, img_1alpha, img_beta), (txt_mu, txt_1alpha, txt_beta) = BayesCap_Net(xfI, xfT)
                
            n_batch = img_mu.shape[0]
            for j in range(n_batch):
                num_imgs += 1
                mean_mse += emb_mse(img_mu[j], xfI[j]) + emb_mse(txt_mu[j], xfT[j])
                mean_mae += emb_mae(img_mu[j], xfI[j]) + emb_mae(txt_mu[j], xfT[j])
            ##
        mean_mse /= num_imgs
        mean_mae /= num_imgs
        print(
            'Avg. MSE: {} | Avg. MAE: {}'.format
            (
                mean_mse, mean_mae 
            )
        )
    return mean_mae, mean_mse

def load_and_evaluate(
    ckpt_path='../ckpt/ProbVLM_Net_best.pth',
    dataset="coco",
    data_dir="../datasets/coco",
    batch_size=64,
    device='cuda'
    ):
    # Load data loaders
    dataloader_config = mch({
        "batch_size": batch_size,
        "random_erasing_prob": 0,
        "traindata_shuffle": True
    })

    loaders = load_data_loader(dataset, data_dir, dataloader_config)
    coco_valid_loader = loaders['val']

    # Load CLIP model
    CLIP_Net = load_model(device=device, model_path=None)

    # Define BayesCap network
    ProbVLM_Net = BayesCap_for_CLIP(
        inp_dim=512,
        out_dim=512,
        hid_dim=256,
        num_layers=3
    )

    # Load the checkpoint
    print(f"Loading checkpoint from {ckpt_path}")
    ProbVLM_Net.load_state_dict(torch.load(ckpt_path, map_location=device))

    # Evaluate using the existing `eval_ProbVLM` function
    mean_mae = eval_ProbVLM(
        CLIP_Net,
        ProbVLM_Net,
        coco_valid_loader,
        device=device
    )

    # Print or calculate additional statistics if needed
    print(f"Mean MAE from evaluation: {mean_mae}")

    # Return the evaluated metrics
    return mean_mae

def eval_ProbVLM_uncert(
    CLIP_Net,
    BayesCap_Net,
    eval_loader,
    device='cuda',
    n_fw=15,  # Number of forward passes for uncertainty estimation
    bins_type='eq_samples',  # 'eq_spacing' or 'eq_samples'
    n_bins=5,  # Number of uncertainty bins,
    retrieval="i2t"
):
    CLIP_Net.to(device)
    CLIP_Net.eval()
    BayesCap_Net.to(device)
    BayesCap_Net.eval()

    # Get features and uncertainties
    r_dict = get_features_uncer_ProbVLM(CLIP_Net, BayesCap_Net, eval_loader)
    
    # Sort samples by uncertainty
    sort_v_idx, sort_t_idx = sort_wrt_uncer(r_dict)
    if retrieval == "i2t":
        sort_idx = sort_v_idx
        query_feature = "ir_f"
        gallery_feature = "tr_f"
    else: # "t2i"
        sort_idx = sort_t_idx
        query_feature = "tr_f"
        gallery_feature = "ir_f"
    # Bin the sorted samples
    if bins_type == 'eq_spacing':
        bins = create_uncer_bins_eq_spacing(sort_idx, n_bins=n_bins)
    elif bins_type == 'eq_samples':
        bins = create_uncer_bins_eq_samples(sort_idx, n_bins=n_bins)
    else:
        raise ValueError("Invalid `bins_type`. Choose 'eq_spacing' or 'eq_samples'.")

    # Calculate recall@1 for each bin
    bin_recalls = []
    counter = 0

    for bin_key, samples in bins.items():
        if not samples:
            bin_recalls.append(0)  # If bin is empty, append 0
            continue
        
        indices = [sample[0] for sample in samples]  # Extract indices from sorted list
        bin_query_features = torch.stack([r_dict[query_feature][i] for i in indices])
        bin_gallery_features = torch.stack(r_dict[gallery_feature])  # All gallery features
        q_classes = [r_dict["classes"][i] for i in indices]
        g_classes = r_dict["classes"]
        assert not torch.isnan(bin_query_features).any(), "NaNs in query features!"
        assert not torch.isnan(bin_gallery_features).any(), "NaNs in gallery features!"
        assert bin_query_features.shape[1] == bin_gallery_features.shape[1], "Feature dimension mismatch!"

        pred_ranks = get_pred_ranks(bin_query_features, bin_gallery_features, recall_ks=(1,))
        assert pred_ranks.flatten().shape[0] == len(indices), "Mismatch in sizes!"
        if counter == 0:
            print("uncertainty for t")
            print(f"Query uncertainty u: {r_dict['t_u'][0]}")
            print(f"Query uncertainty au: {r_dict['t_au'][0]}")
            print(f"Query uncertainty eu: {r_dict['t_eu'][0]}")
            #print("indicies", len(indices))
        counter += 1
        recall_scores = new_recall(pred_ranks_all=pred_ranks, recall_ks=(1,), q_classes_all=q_classes, g_classes_all=g_classes)
        #recall_scores = get_recall(pred_ranks, recall_ks=(1,))
        #recall_1 = get_recall_at_1(pred_ranks, indices)
        bin_recalls.append(recall_scores[0])
        #bin_recalls.append(recall_1)
    return bin_recalls, bins


def load_and_evaluate_uncert(
    ckpt_path='../ckpt/ProbVLM_Net_best.pth',
    dataset="coco",
    data_dir="../datasets/coco",
    batch_size=64,
    device='cuda',
    n_bins=5,
    bins_type='eq_samples',
    model_type="ProbVLM",
    retrieval = "i2t"
):
    # Load data loaders
    dataloader_config = mch({
        "batch_size": batch_size,
        "random_erasing_prob": 0,
        "traindata_shuffle": True
    })

    loaders = load_data_loader(dataset, data_dir, dataloader_config)
    coco_valid_loader = loaders['val']

    # Load CLIP model
    CLIP_Net = load_model(device=device, model_path=None)

    # Define BayesCap network
    if model_type == "BBB":
        ProbVLM_Net = BayesCap_for_CLIP_BBB_Enc(
            inp_dim=512,
            out_dim=512,
            hid_dim=256,
            num_layers=3
        )
    elif model_type == "ProbVLM":
        ProbVLM_Net = BayesCap_for_CLIP_ProbVLM(
            inp_dim=512,
            out_dim=512,
            hid_dim=256,
            num_layers=3
        )

    # Load the checkpoint
    print(f"Loading checkpoint from {ckpt_path}")
    ProbVLM_Net.load_state_dict(torch.load(ckpt_path, map_location=device))

    # Evaluate with uncertainty
    bin_recalls, bins = eval_ProbVLM_uncert(
        CLIP_Net,
        ProbVLM_Net,
        coco_valid_loader,
        device=device,
        n_bins=n_bins,
        bins_type=bins_type,
        retrieval=retrieval
    )

    # Plot recall@1 vs uncertainty bins
    bin_labels = [f'Bin {i+1}' for i in range(len(bin_recalls))]
    plt.figure(figsize=(10, 6))
    plt.plot(bin_labels, bin_recalls, color='skyblue')
    plt.xlabel('Uncertainty Bins')
    plt.ylabel('Recall@1')
    plt.title('Recall@1 vs. Binned Uncertainty Levels')
    plt.xticks(rotation=45)
    plt.grid(axis='y', linestyle='--', alpha=0.7)
    plt.tight_layout()
    plt.savefig("recall_plot.png")

    return bin_recalls, bins

def main():
    #model = "../ckpt/BBB_woKL_Net_best.pth"
    model = "../ckpt/ProbVLM_Net_best_3.pth"
    #model = "../ckpt/BBB_EncBL_best_3.pth"
    #eval_results = load_and_evaluate()
    #print(eval_results)
    bin_recalls, bins = load_and_evaluate_uncert(ckpt_path=model, bins_type="eq_samples", model_type="ProbVLM", retrieval="t2i")
    print("Text to image, equal samples ProbVLM_Net_best_3. Estimate of epistemic: sum of all no division, include aleatoric. Using new_recall().")
    print("Recall@1 for each bin:", bin_recalls)
    bin_recalls, bins = load_and_evaluate_uncert(ckpt_path=model, bins_type="eq_samples", model_type="ProbVLM", retrieval="i2t")
    print("Image to text, equal samples ProbVLM_Net_best_3. Estimate of epistemic: sum of all no division, include aleatoric. Using new_recall().")
    print("Recall@1 for each bin:", bin_recalls)

    #iod_ood = compare_iod_vs_ood(ckpt_path=model)


if __name__ == "__main__":
    main()

