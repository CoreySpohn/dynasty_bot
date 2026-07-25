"""Reading and writing hand-maintained YAML config without destroying it.

`yaml.safe_load` throws comments away, so any config the bot both reads and
writes loses its documentation the first time it's saved. That's how all 16
comment lines disappeared from `deadlines.yaml`: the anchor sync round-tripped
the whole file through `yaml.dump`. Generated dates have since moved to their
own file, but the same hazard remains wherever the bot edits human config -
`/deadline` commands, and `_save_state` when the league state advances, which
happens automatically.

ruamel's round-trip mode keeps comments, key order, quoting and blank lines
attached to the loaded object, so a load-mutate-save cycle only changes what was
actually mutated. The catch is that it only works when the *same* object is
saved: comments live on the CommentedMap that `load` returns, so `load_config`
and `save_config` have to be used as a pair.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from ruamel.yaml import YAML

logger = logging.getLogger("dynasty_bot.yaml_config")

# Wide enough that long Discord descriptions aren't re-wrapped mid-sentence,
# which would show up as noise in every diff.
_MAX_WIDTH = 4096


def _represent_none(representer, _data):
    """Emit `null` rather than an empty value.

    ruamel writes `date:` for None by default. That parses back identically, but
    these files are hand-edited and version-controlled, so the first save would
    rewrite every null line and bury the real change in noise. Being explicit
    keeps an unchanged save byte-identical, which the tests assert.
    """
    return representer.represent_scalar("tag:yaml.org,2002:null", "null")


def _round_tripper() -> YAML:
    yaml = YAML()  # round-trip is the default mode
    yaml.preserve_quotes = True
    yaml.width = _MAX_WIDTH
    yaml.representer.add_representer(type(None), _represent_none)
    return yaml


def load_config(path: Path, default: Optional[dict[str, Any]] = None) -> Any:
    """Load YAML preserving comments, or `default` if unreadable.

    The returned object behaves like a dict but carries the file's formatting.
    Pass it back to `save_config` to keep that formatting.
    """
    try:
        with open(path) as f:
            loaded = _round_tripper().load(f)
    except FileNotFoundError:
        logger.warning(f"{path.name} not found, using defaults")
        return {} if default is None else default
    except Exception as e:
        logger.error(f"Could not parse {path.name}: {e}")
        return {} if default is None else default

    return {} if loaded is None else loaded


def save_config(data: Any, path: Path) -> None:
    """Write YAML, preserving whatever comments came with `data`.

    Writes via a temporary file in the same directory and replaces atomically,
    so an interrupted write can't leave a half-truncated config behind - these
    files gate reminders and league state, and a corrupt one fails at startup.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w") as f:
            _round_tripper().dump(data, f)
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()
