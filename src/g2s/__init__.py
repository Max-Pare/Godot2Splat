"""Godot 3D scene to Gaussian splat, and back into the Godot project."""

from .gsplat_compat import point_to_gpu_id_table, use_system_rocm_runtime

# Both must happen before torch is imported.
point_to_gpu_id_table()
use_system_rocm_runtime()
