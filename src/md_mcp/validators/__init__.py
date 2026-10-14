from .runner import ValidatorInfo, ValidatorRunner, available_validators, count_severities

# Whole-mod cross-reference scans, minutes not seconds. Excluded from run-all
# paths ("*" in lint, validator=None in validate); reachable by explicit name.
SLOW_VALIDATORS: frozenset = frozenset({"unused_scripted", "unused_textures"})

# These require a user-selected full sweep or commit-stage context. They remain
# available by explicit name, but should not surprise callers of run-all.
IMPLICIT_RUN_ALL_EXCLUDED: frozenset = frozenset({"standardization", "mesh_textures"})

# Shared severity ordering for `severity_min` floor filters in lint/validate tools.
SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}

__all__ = [
    "IMPLICIT_RUN_ALL_EXCLUDED",
    "SEVERITY_RANK",
    "SLOW_VALIDATORS",
    "ValidatorInfo",
    "ValidatorRunner",
    "available_validators",
    "count_severities",
]
