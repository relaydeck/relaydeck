"""
Theme engine — named, extendable bundles of dashboard design tokens.

A *theme* is a named override of the dashboard's CSS custom properties
(the ~50 `--*` tokens defined in `web/static/styles.css` `:root`). A theme
may set any subset of those tokens; anything it leaves unset falls through
to the base (`:root`) value, so a theme is purely additive layering. A
theme may also `extends:` another theme, inheriting its tokens and
overriding a few — the resolver walks the chain (cycle-guarded) and
flattens it to a single token map the client applies as inline
`style.setProperty` calls.

Themes live in `~/.relaydeck/themes/<name>.yaml`, one file per
theme — same on-disk discipline as `layouts.py`: filename-sanitized,
mode 0600, atomic temp+rename, defensive load. A handful of *builtin*
themes ship in-package (the accent presets that previously lived only as
`[data-accent]` CSS rules, formalized here so they appear in the gallery
and can be `extends:`-ed). Builtins are never written to disk and cannot
be deleted; a user file of the same name *shadows* a builtin, so
`amber` can be customized and reset (delete the file → builtin returns).

The package ships theme *slots and a couple of starting points*, never a
mandated look — the system default is `:root` itself (the empty `base`
theme), so zero-config boot is unchanged.

THIS MODULE IS THE TOKEN CONTRACT. `THEME_TOKENS` is the authoritative
list of what a theme may set; the dashboard editor, the API validator,
and the bundled `theme` skill all read from it so they never drift.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Token contract ─────────────────────────────────────────────────
#
# The authoritative set of CSS custom properties a theme may override,
# mirroring `web/static/styles.css` `:root`. `name` is the property
# WITHOUT the leading `--`. `type` drives the editor widget
# (color picker / text size / font stack). Keep in sync with styles.css
# — a token here that doesn't exist in the stylesheet is harmless (it
# just sets an unused var); a stylesheet token missing here can't be
# themed.


@dataclass(frozen=True)
class TokenDef:
    name: str           # CSS var name without the leading `--`
    label: str          # human label for the editor
    category: str       # grouping in the editor
    type: str           # "color" | "size" | "font"
    default: str        # the :root value (for the reset affordance + preview)


THEME_TOKENS: list[TokenDef] = [
    # surfaces — warm paper canvas (Studio redesign; mirrors styles.css :root)
    TokenDef("bg-0", "Canvas", "Surfaces", "color", "#F2EFE6"),
    TokenDef("bg-1", "Elevation 1", "Surfaces", "color", "#FBF8F0"),
    TokenDef("bg-2", "Elevation 2", "Surfaces", "color", "#E6E1D1"),
    TokenDef("bg-3", "Elevation 3", "Surfaces", "color", "#DED7C5"),
    TokenDef("bg-4", "Elevation 4", "Surfaces", "color", "#D5CEB9"),
    TokenDef("bg-term", "Terminal bg", "Surfaces", "color", "#14120D"),
    # hairlines
    TokenDef("line-1", "Hairline subtle", "Lines", "color", "#E4DECB"),
    TokenDef("line-2", "Hairline", "Lines", "color", "#DED8C6"),
    TokenDef("line-3", "Hairline strong", "Lines", "color", "#D6D0BD"),
    TokenDef("line-4", "Hairline bold", "Lines", "color", "#C9C2AC"),
    # text — warm inks
    TokenDef("t-1", "Text primary", "Text", "color", "#15130E"),
    TokenDef("t-2", "Text secondary", "Text", "color", "#46423a"),
    TokenDef("t-3", "Text muted", "Text", "color", "#756F60"),
    TokenDef("t-4", "Text disabled", "Text", "color", "#9a937e"),
    # accent — single terracotta, no glow
    TokenDef("acc", "Accent", "Accent", "color", "#B7410E"),
    TokenDef("acc-d", "Accent deep", "Accent", "color", "#9E380C"),
    TokenDef("acc-soft", "Accent soft", "Accent", "color", "rgba(183,65,14,.08)"),
    TokenDef("acc-line", "Accent border", "Accent", "color", "rgba(183,65,14,.30)"),
    TokenDef("acc-glow", "Accent glow", "Accent", "color", "transparent"),
    TokenDef("acc-text", "On-accent text", "Accent", "color", "#FBF8F0"),
    # status — muted, earthy
    TokenDef("ok", "Success", "Status", "color", "#3F6B3A"),
    TokenDef("ok-soft", "Success soft", "Status", "color", "rgba(63,107,58,.12)"),
    TokenDef("warn", "Warning", "Status", "color", "#A36912"),
    TokenDef("warn-soft", "Warning soft", "Status", "color", "rgba(163,105,18,.13)"),
    TokenDef("err", "Error", "Status", "color", "#8B2818"),
    TokenDef("err-soft", "Error soft", "Status", "color", "rgba(139,40,24,.10)"),
    TokenDef("info", "Info", "Status", "color", "#2F6F8F"),
    TokenDef("info-soft", "Info soft", "Status", "color", "rgba(47,111,143,.12)"),
    TokenDef("pink", "Pink", "Status", "color", "#A8456B"),
    TokenDef("violet", "Violet", "Status", "color", "#6D5BA8"),
    # type — IBM Plex Sans + Mono
    TokenDef("f-sans", "Sans family", "Type", "font",
             "'IBM Plex Sans', ui-sans-serif, -apple-system, system-ui, sans-serif"),
    TokenDef("f-mono", "Mono family", "Type", "font",
             "'IBM Plex Mono', 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace"),
    TokenDef("t-xxs", "Size xxs", "Type", "size", "10px"),
    TokenDef("t-xs", "Size xs", "Type", "size", "11px"),
    TokenDef("t-sm", "Size sm", "Type", "size", "12px"),
    TokenDef("t-md", "Size md", "Type", "size", "13px"),
    TokenDef("t-lg", "Size lg", "Type", "size", "15px"),
    TokenDef("t-xl", "Size xl", "Type", "size", "18px"),
    TokenDef("t-2xl", "Size 2xl", "Type", "size", "22px"),
    TokenDef("t-3xl", "Size 3xl", "Type", "size", "32px"),
    TokenDef("t-display", "Display", "Type", "size", "56px"),
    # radii
    TokenDef("r-0", "Radius 0", "Radii", "size", "2px"),
    TokenDef("r-1", "Radius 1", "Radii", "size", "4px"),
    TokenDef("r-2", "Radius 2", "Radii", "size", "6px"),
    TokenDef("r-3", "Radius 3", "Radii", "size", "8px"),
    TokenDef("r-4", "Radius 4", "Radii", "size", "12px"),
    # spacing / density (orthogonal to the data-density quick toggle, but
    # a theme MAY bake spacing in; inline vars win over the attribute)
    TokenDef("row-h", "Row height", "Spacing", "size", "28px"),
    TokenDef("pad-x", "Pad x", "Spacing", "size", "14px"),
    TokenDef("pad-y", "Pad y", "Spacing", "size", "10px"),
    TokenDef("tab-h", "Tab height", "Spacing", "size", "32px"),
    TokenDef("hdr-h", "Header height", "Spacing", "size", "44px"),
    TokenDef("bar-h", "Status bar", "Spacing", "size", "26px"),
    TokenDef("side-w", "Sidebar width", "Spacing", "size", "280px"),
]

TOKEN_NAMES: set[str] = {t.name for t in THEME_TOKENS}
TOKEN_DEFAULTS: dict[str, str] = {t.name: t.default for t in THEME_TOKENS}

# Defensive caps: a token value is a CSS literal applied via
# style.setProperty (which ignores anything malformed), but we still
# bound length and reject control chars so a hand-edited theme file
# can't bloat the inline style or smuggle newlines into the payload.
_MAX_VALUE_LEN = 200


@dataclass
class Theme:
    name: str
    display_name: str = ""
    description: str = ""
    author: str = ""
    extends: str | None = None
    tokens: dict[str, str] = field(default_factory=dict)
    builtin: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # `builtin` is derived (a user file is never builtin), don't persist.
        d.pop("builtin", None)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, builtin: bool = False) -> Theme:
        raw_tokens = data.get("tokens") or {}
        tokens: dict[str, str] = {}
        if isinstance(raw_tokens, dict):
            for k, v in raw_tokens.items():
                if k in TOKEN_NAMES and isinstance(v, str):
                    tokens[k] = v[:_MAX_VALUE_LEN]
        extends = data.get("extends")
        return cls(
            name=str(data.get("name") or ""),
            display_name=str(data.get("display_name") or ""),
            description=str(data.get("description") or ""),
            author=str(data.get("author") or ""),
            extends=str(extends) if extends else None,
            tokens=tokens,
            builtin=builtin,
        )


# ── Builtin themes ─────────────────────────────────────────────────
#
# `base` is the empty theme: applying it sets NO inline vars, so the
# dashboard renders exactly as `:root`. The accent presets formalize the
# old `[data-accent]` CSS rules so they show in the gallery + can be
# extended. These are *starting points*, not a mandated look.


def _accent(acc: str, acc_d: str, soft: str, line: str, glow: str) -> dict[str, str]:
    return {
        "acc": acc, "acc-d": acc_d, "acc-soft": soft,
        "acc-line": line, "acc-glow": glow, "acc-text": "#04070a",
    }


BUILTIN_THEMES: dict[str, Theme] = {
    t.name: t for t in [
        Theme("base", "Paper (system)", "The shipped warm-paper canvas — the default.",
              "relaydeck", None, {}, builtin=True),
        # Ink — the dark sibling of the paper default (Studio redesign). Warm
        # near-black surfaces, terracotta accent. Completes paper/ink/mono.
        Theme("ink", "Ink", "Warm dark sibling of the paper default.",
              "relaydeck", "base", {
                  "bg-0": "#13120E", "bg-1": "#1B1914", "bg-2": "#0D0C09",
                  "bg-3": "#232019", "bg-4": "#2A2720",
                  "line-1": "#1F1D17", "line-2": "#232019",
                  "line-3": "#2A2720", "line-4": "#3A362B",
                  "t-1": "#ECE7DA", "t-2": "#9E998A", "t-3": "#6A6557", "t-4": "#454135",
                  "acc": "#E5743A", "acc-d": "#C75A28",
                  "acc-soft": "rgba(229,116,58,.12)", "acc-line": "rgba(229,116,58,.32)",
                  "acc-glow": "transparent", "acc-text": "#13120E",
                  "ok": "#6FA77E", "ok-soft": "rgba(111,167,126,.14)",
                  "warn": "#D4972C", "warn-soft": "rgba(212,151,44,.14)",
                  "err": "#C45A3F", "err-soft": "rgba(196,90,63,.14)",
                  "info": "#5E97B0", "info-soft": "rgba(94,151,176,.14)",
                  "pink": "#C98AA0", "violet": "#9A8AC9",
              }, builtin=True),
        Theme("cyan", "Cyan", "Default cyan accent.", "relaydeck", "base",
              _accent("#67e8f9", "#22d3ee", "rgba(103,232,249,.10)",
                      "rgba(103,232,249,.30)", "rgba(103,232,249,.22)"), builtin=True),
        Theme("green", "Green", "Green accent.", "relaydeck", "base",
              _accent("#6ee7a3", "#34d399", "rgba(110,231,163,.10)",
                      "rgba(110,231,163,.30)", "rgba(110,231,163,.22)"), builtin=True),
        Theme("amber", "Amber", "Amber accent — warm warning palette.", "relaydeck",
              "base", _accent("#fbbf24", "#f59e0b", "rgba(251,191,36,.10)",
                              "rgba(251,191,36,.30)", "rgba(251,191,36,.22)"), builtin=True),
        Theme("violet", "Violet", "Violet accent.", "relaydeck", "base",
              _accent("#a78bfa", "#8b5cf6", "rgba(167,139,250,.12)",
                      "rgba(167,139,250,.35)", "rgba(167,139,250,.25)"), builtin=True),
        Theme("mono", "Mono", "Monochrome — restrained, near-greyscale accent.",
              "relaydeck", "base",
              _accent("#e8e9ee", "#c8cad2", "rgba(232,233,238,.07)",
                      "rgba(232,233,238,.25)", "rgba(232,233,238,.15)"), builtin=True),
        # ── Full-palette themes (not just accent swaps) ───────────────
        # Gruvbox Dark — the retro warm earth-tone scheme with a cult dev
        # following. Recolors the whole surface stack (warm dark, not the
        # cold pure-black of base), proving the engine does more than swap
        # an accent. Palette: morhetz/gruvbox (dark medium), orange accent.
        Theme("gruvbox-dark", "Gruvbox Dark",
              "Retro warm earth tones — the cult terminal/Vim palette.",
              "relaydeck", "base", {
                  "bg-0": "#1d2021", "bg-1": "#282828", "bg-2": "#32302f",
                  "bg-3": "#3c3836", "bg-4": "#504945",
                  "line-1": "#32302f", "line-2": "#3c3836",
                  "line-3": "#504945", "line-4": "#665c54",
                  "t-1": "#ebdbb2", "t-2": "#d5c4a1", "t-3": "#a89984", "t-4": "#7c6f64",
                  "acc": "#fe8019", "acc-d": "#d65d0e",
                  "acc-soft": "rgba(254,128,25,.13)", "acc-line": "rgba(254,128,25,.32)",
                  "acc-glow": "rgba(254,128,25,.22)", "acc-text": "#1d2021",
                  "ok": "#b8bb26", "ok-soft": "rgba(184,187,38,.14)",
                  "warn": "#fabd2f", "warn-soft": "rgba(250,189,47,.15)",
                  "err": "#fb4934", "err-soft": "rgba(251,73,52,.14)",
                  "info": "#83a598", "info-soft": "rgba(131,165,152,.15)",
                  "pink": "#d3869b", "violet": "#d3869b",
              }, builtin=True),
        # Daylight — a clean LIGHT theme for bright environments / daytime /
        # operators who don't want a dark UI. The dashboard is dark-native,
        # so this fully inverts the surface + text stack and flips
        # acc-text to white (the accent is now a dark-on-light blue).
        Theme("daylight", "Daylight",
              "Clean light theme for bright rooms and daytime ops.",
              "relaydeck", "base", {
                  "bg-0": "#eceef1", "bg-1": "#f3f4f6", "bg-2": "#f9fafb",
                  "bg-3": "#ffffff", "bg-4": "#ffffff",
                  "line-1": "#e3e5e9", "line-2": "#d6d9df",
                  "line-3": "#c2c6ce", "line-4": "#a8adb8",
                  "t-1": "#1a1d23", "t-2": "#454b57", "t-3": "#6b7280", "t-4": "#9aa1ad",
                  "acc": "#2563eb", "acc-d": "#1d4ed8",
                  "acc-soft": "rgba(37,99,235,.10)", "acc-line": "rgba(37,99,235,.28)",
                  "acc-glow": "rgba(37,99,235,.16)", "acc-text": "#ffffff",
                  "ok": "#15803d", "ok-soft": "rgba(21,128,61,.12)",
                  "warn": "#b45309", "warn-soft": "rgba(180,83,9,.13)",
                  "err": "#b91c1c", "err-soft": "rgba(185,28,28,.11)",
                  "info": "#1d4ed8", "info-soft": "rgba(29,78,216,.11)",
                  "pink": "#be185d", "violet": "#6d28d9",
              }, builtin=True),
    ]
}

DEFAULT_THEME = "base"


# ── Paths + sanitize (mirrors layouts.py) ──────────────────────────


def _config_home(config_home: Path | None = None) -> Path:
    """relaydeck-wide convention: explicit arg wins, else
    `$RELAYDECK_CONFIG_HOME`, else `~/.relaydeck` (mirrors
    model_roles/skills so the CLI + tests resolve the same root)."""
    if config_home is not None:
        return config_home
    override = os.environ.get("RELAYDECK_CONFIG_HOME")
    return Path(override) if override else (Path.home() / ".relaydeck")


def _themes_dir(config_home: Path | None = None) -> Path:
    return _config_home(config_home) / "themes"


def _theme_path(name: str, config_home: Path | None = None) -> Path:
    return _themes_dir(config_home) / f"{_sanitize_name(name)}.yaml"


def _sanitize_name(name: str) -> str:
    """Themes map 1:1 to files — keep names filename-safe so a crafted
    name can only write under the themes directory."""
    keep = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
    return "".join(c if c in keep else "_" for c in name).strip("._") or "unnamed"


# ── Validation ─────────────────────────────────────────────────────


def validate_tokens(tokens: dict[str, Any]) -> dict[str, str]:
    """Return only known tokens with sane string values; raise on
    unknown keys so a typo doesn't silently no-op in the editor."""
    if not isinstance(tokens, dict):
        raise ValueError("tokens must be a mapping")
    out: dict[str, str] = {}
    for k, v in tokens.items():
        if k not in TOKEN_NAMES:
            raise ValueError(f"unknown token: {k!r}")
        if not isinstance(v, str):
            raise ValueError(f"token {k!r} value must be a string")
        if len(v) > _MAX_VALUE_LEN or "\n" in v or "\r" in v:
            raise ValueError(f"token {k!r} value invalid (too long or has newlines)")
        out[k] = v
    return out


def _cycle(start: str, get: Any) -> bool:
    """True if following `extends` from `start` loops. `get(name)`
    returns the parent name or None."""
    seen: set[str] = set()
    cur: str | None = start
    while cur:
        if cur in seen:
            return True
        seen.add(cur)
        cur = get(cur)
    return False


# ── Public API ─────────────────────────────────────────────────────


def get_theme(name: str, *, config_home: Path | None = None) -> Theme | None:
    """A user file shadows a builtin of the same name. Returns None if
    neither exists."""
    import yaml

    path = _theme_path(name, config_home)
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text()) or {}
            if isinstance(data, dict):
                data.setdefault("name", _sanitize_name(name))
                return Theme.from_dict(data, builtin=False)
        except Exception as exc:
            logger.warning("theme %s load failed: %s", name, exc)
    return BUILTIN_THEMES.get(name)


def list_themes(*, config_home: Path | None = None) -> list[Theme]:
    """Builtins + user themes, user shadowing builtin, sorted by name."""
    by_name: dict[str, Theme] = dict(BUILTIN_THEMES)
    d = _themes_dir(config_home)
    if d.exists():
        for child in sorted(d.iterdir()):
            if child.is_file() and child.suffix in (".yaml", ".yml"):
                t = get_theme(child.stem, config_home=config_home)
                if t is not None:
                    by_name[child.stem] = t
    return [by_name[k] for k in sorted(by_name)]


def resolve_theme(name: str, *, config_home: Path | None = None) -> dict[str, str]:
    """Flatten a theme + its `extends` chain into a single token map.
    Base-most tokens first, each level overriding. Unknown / missing
    themes resolve to the empty map (== :root). Cycle-safe."""
    chain: list[Theme] = []
    seen: set[str] = set()
    cur: str | None = name
    while cur and cur not in seen:
        seen.add(cur)
        t = get_theme(cur, config_home=config_home)
        if t is None:
            break
        chain.append(t)
        cur = t.extends
    merged: dict[str, str] = {}
    for t in reversed(chain):  # base → leaf
        merged.update(t.tokens)
    return merged


def save_theme(theme: Theme, *, config_home: Path | None = None) -> Path:
    """Persist a user theme. Validates tokens + guards against an
    `extends` cycle through the candidate. Atomic, mode 0600."""
    import yaml

    if theme.name in BUILTIN_THEMES and theme.name == _sanitize_name(theme.name):
        # Allowed: a user file may shadow a builtin to customize it. We
        # only block creating a *file* that would collide with a builtin
        # if it would also be unresolvable — but shadowing is the
        # intended reset story, so permit it.
        pass

    theme.tokens = validate_tokens(theme.tokens)

    if theme.extends:
        # Build a lookup that reflects the post-save world: this theme's
        # extends + everyone else's on-disk/builtin extends.
        def parent(n: str) -> str | None:
            if n == theme.name:
                return theme.extends
            t = get_theme(n, config_home=config_home)
            return t.extends if t else None

        if _cycle(theme.name, parent):
            raise ValueError(f"extends cycle through {theme.name!r}")

    path = _theme_path(theme.name, config_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(theme.to_dict(), sort_keys=True, default_flow_style=False)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def delete_theme(name: str, *, config_home: Path | None = None) -> bool:
    """Remove a user theme file. A pure builtin (no shadowing file)
    refuses — there's nothing to delete and we won't pretend. Deleting a
    file that shadows a builtin reverts to the builtin."""
    path = _theme_path(name, config_home)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError as exc:
        logger.warning("theme %s delete failed: %s", name, exc)
        return False


def is_builtin(name: str) -> bool:
    return name in BUILTIN_THEMES
