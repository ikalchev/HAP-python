import os

# Absolutize paths to coverage config and output file because tests that
# spawn subprocesses also changes current working directory.
_sourceroot = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if "COV_CORE_CONFIG" in os.environ:
    os.environ["COVERAGE_FILE"] = os.path.join(_sourceroot, ".coverage")
    os.environ["COV_CORE_CONFIG"] = os.path.join(
        _sourceroot, os.environ["COV_CORE_CONFIG"]
    )
