"""Import g2s before any test module imports torch (see gsplat_compat.use_system_rocm_runtime)."""
import g2s  # noqa: F401
