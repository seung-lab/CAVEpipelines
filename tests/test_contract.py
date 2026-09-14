"""The contract records the operator builds its commands from."""

import pytest

from cave_pipeline import contract


def test_setup_argv_refuses_what_no_image_is_checked_for():
    """preflight checks an image only for the declared interface, so sending anything else
    would reach a setup nothing verified accepts it."""
    ingest, meshing = contract.WORKLOADS["ingest"], contract.WORKLOADS["meshing"]
    assert ingest.setup_argv("g", flags=(contract.RAW_FLAG,))[-2:] == ["g", "--raw"]
    with pytest.raises(ValueError, match="declares no --raw"):
        meshing.setup_argv("g", flags=(contract.RAW_FLAG,))
    with pytest.raises(ValueError, match="takes exactly"):
        ingest.setup_argv("g", "extra")
