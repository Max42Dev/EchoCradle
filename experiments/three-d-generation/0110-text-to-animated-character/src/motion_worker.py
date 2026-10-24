"""Generate real local MoMask joint motion and BVH, without SMPL or video models."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text())
    frames = spec["frames"]
    if type(frames) is not int or not 4 <= frames <= 196 or frames % 4:
        raise ValueError("Motion frames must be 4..196 and divisible by four")
    if spec["fps"] != 20 or not isinstance(spec["prompt"], str) or len(spec["prompt"]) > 1000:
        raise ValueError("Unsupported motion specification")
    import numpy as np
    import torch
    import clip

    # Upstream old NumPy aliases, scoped to this worker process.
    np.float = float
    np.int = int
    # Upstream only uses the removed testing module for batched matrix multiply.
    matrix_compat = types.ModuleType("numpy.core.umath_tests")
    matrix_compat.matrix_multiply = np.matmul
    sys.modules["numpy.core.umath_tests"] = matrix_compat
    original_load = clip.load

    def local_clip(name: str, *positional: object, **kwargs: object) -> object:
        return original_load(str(args.models / "ViT-B-32.pt"), *positional, **kwargs)

    clip.load = local_clip
    os.environ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = "1"
    sys.path.insert(0, str(args.checkout))
    os.chdir(args.checkout)
    from gen_t2m import load_vq_model, load_trans_model, load_res_model
    from utils.get_opt import get_opt
    from utils.fixseed import fixseed
    from utils.motion_process import recover_from_ric
    from visualization.joints2bvh import Joint2BVHConvertor

    fixseed(spec["seed"])
    start = time.monotonic()
    torch.cuda.reset_peak_memory_stats()
    device = torch.device("cuda:0")
    mask_name = "t2m_nlayer8_nhead6_ld384_ff1024_cdp0.1_rvq6ns"
    residual_name = "tres_nlayer8_ld384_ff1024_rvq6ns_cdp0.2_sw"
    options = SimpleNamespace(device=device, checkpoints_dir=str(args.models),
                              dataset_name="t2m", name=mask_name, res_name=residual_name)
    mask_opt = get_opt(str(args.models / "t2m" / mask_name / "opt.txt"), device=device,
                       checkpoints_dir=str(args.models))
    vq_root = args.models / "t2m" / mask_opt.vq_name
    vq_opt = get_opt(str(vq_root / "opt.txt"), device=device, checkpoints_dir=str(args.models))
    vq_opt.dim_pose = 263
    vq, vq_opt = load_vq_model(vq_opt)
    mask_opt.num_tokens = vq_opt.nb_code
    mask_opt.num_quantizers = vq_opt.num_quantizers
    mask_opt.code_dim = vq_opt.code_dim
    residual_opt = get_opt(str(args.models / "t2m" / residual_name / "opt.txt"),
                           device=device, checkpoints_dir=str(args.models))
    residual = load_res_model(residual_opt, vq_opt, options)
    mask = load_trans_model(mask_opt, options, "latest.tar")
    for model in (vq, residual, mask):
        model.eval().to(device)
    mean = np.load(vq_root / "meta/mean.npy", allow_pickle=False)
    std = np.load(vq_root / "meta/std.npy", allow_pickle=False)
    lengths = torch.tensor([frames // 4], device=device, dtype=torch.long)
    with torch.no_grad():
        indices = mask.generate([spec["prompt"]], lengths, timesteps=18, cond_scale=4,
                                temperature=1, topk_filter_thres=0.9, gsample=False)
        indices = residual.generate(indices, [spec["prompt"]], lengths,
                                    temperature=1, cond_scale=5)
        decoded = vq.forward_decoder(indices).cpu().numpy()[0, :frames]
    joints = recover_from_ric(torch.from_numpy(decoded * std + mean).float(), 22).numpy()
    if joints.shape != (frames, 22, 3) or not np.isfinite(joints).all():
        raise RuntimeError("Invalid generated joint sequence")
    args.output.mkdir(parents=True, exist_ok=True)
    np.save(args.output / "motion_joints.npy", joints)
    converter = Joint2BVHConvertor()
    _, fitted = converter.convert(joints, filename=str(args.output / "motion.bvh"),
                                  iterations=100, foot_ik=False)
    np.save(args.output / "motion_bvh_joints.npy", fitted)
    report = {"spec": spec, "frames": frames, "joints": 22, "fps": 20,
              "seconds": time.monotonic() - start,
              "root_travel": float(np.linalg.norm(joints[-1, 0] - joints[0, 0])),
              "maximum_frame_joint_displacement": float(np.linalg.norm(
                  np.diff(joints, axis=0), axis=2).max()),
              "bvh_fit_rmse": float(np.sqrt(np.mean((fitted - joints) ** 2))),
              "cuda_peak_allocated_bytes": torch.cuda.max_memory_allocated(),
              "commercial_clearance": "not_established", "foot_contact": "not_verified"}
    (args.output / "motion_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()