from .runner import ValidatorInfo, ValidatorRunner, available_validators

# Whole-mod cross-reference scans, minutes not seconds. Excluded from run-all
# paths ("*" in lint, validator=None in validate); reachable by explicit name.
SLOW_VALIDATORS: frozenset = frozenset({"unused_scripted", "unused_textures"})

# Shared severity ordering for `severity_min` floor filters in lint/validate tools.
SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}

__all__ = [
    "SEVERITY_RANK",
    "SLOW_VALIDATORS",
    "ValidatorInfo",
    "ValidatorRunner",
    "available_validators",
]
