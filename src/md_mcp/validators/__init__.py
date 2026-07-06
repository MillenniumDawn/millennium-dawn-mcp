from .runner import ValidatorInfo, ValidatorRunner, available_validators

# Whole-mod cross-reference scans, minutes not seconds. Excluded from run-all
# paths ("*" in lint, validator=None in validate); reachable by explicit name.
SLOW_VALIDATORS: frozenset = frozenset({"unused_scripted", "unused_textures"})

__all__ = ["SLOW_VALIDATORS", "ValidatorInfo", "ValidatorRunner", "available_validators"]
