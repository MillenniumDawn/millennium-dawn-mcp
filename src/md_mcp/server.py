"""md-mcp server entrypoint.

Registers all tools and resources, wiring them to the lazy-built indexes and the
validator wrapper.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import replace
from typing import Optional

from .analysis.diff_summary import diff_summary
from .analysis.encoding import check_encoding
from .analysis.focus_graph import focus_graph
from .analysis.focus_layout import focus_layout
from .analysis.manifest import list_country_content
from .analysis.ref_audit import check_refs
from .analysis.refs import find_references
from .config import Settings, load
from .generators import (
    generate_decision,
    generate_event,
    generate_focus,
    generate_gfx_entry,
    generate_gfx_merge,
    generate_idea,
    generate_loc_stub,
)
from .indexes import (
    DecisionIndex,
    EventIndex,
    FocusIndex,
    GfxIndex,
    IdeaIndex,
    LocalisationIndex,
)
from .resources import (
    decision_resource,
    event_resource,
    focus_resource,
    idea_resource,
    loc_resource,
    sprite_resource,
)
from .tools.analysis_tools import find_focuses_tool
from .tools.linting_tools import lint_tool, review_branch_tool
from .tools.parser_tools import parse_file_tool, parse_string_tool
from .tools.resolver_tools import (
    resolve_decision_tool,
    resolve_event_tool,
    resolve_focus_tool,
    resolve_idea_tool,
    resolve_loc_tool,
    resolve_sprite_tool,
)
from .tools.validation_tools import validate_list_tool, validate_tool
from .validators import ValidatorRunner

logger = logging.getLogger("md_mcp")


def build_server(settings: Settings):
    """Construct the FastMCP server with all tools and resources registered.

    Returns the FastMCP instance.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # pragma: no cover
        raise SystemExit(
            "The `mcp` package is not installed. Run `pip install -e .` from the "
            "millennium-dawn-mcp directory."
        ) from e

    mcp = FastMCP("md-mcp")
    focus_index = FocusIndex(settings.mod_root, settings.cache_dir, settings.vanilla_path)
    loc_index = LocalisationIndex(settings.mod_root, settings.cache_dir, settings.vanilla_path)
    gfx_index = GfxIndex(settings.mod_root, settings.cache_dir, settings.vanilla_path)
    event_index = EventIndex(settings.mod_root, settings.cache_dir, settings.vanilla_path)
    decision_index = DecisionIndex(settings.mod_root, settings.cache_dir, settings.vanilla_path)
    idea_index = IdeaIndex(settings.mod_root, settings.cache_dir, settings.vanilla_path)
    validator_runner = ValidatorRunner(settings.mod_root, mode=settings.validator_mode)

    # ---------- resolvers ----------

    @mcp.tool()
    def resolve_focus(focus_id: str) -> dict:
        """Get a focus's file, line, and parsed metadata (x, y, cost, icon, prereqs, mutex) by ID."""
        return resolve_focus_tool(focus_id, settings, focus_index)

    @mcp.tool()
    def resolve_loc(key: str, lang: Optional[str] = None) -> dict:
        """Get a localisation key's value, file, and line. Falls back to English if missing in `lang`."""
        return resolve_loc_tool(key, settings, loc_index, lang)

    @mcp.tool()
    def resolve_sprite(name: str) -> dict:
        """Get a GFX sprite's .gfx file, line, and texture path by name."""
        return resolve_sprite_tool(name, settings, gfx_index)

    @mcp.tool()
    def resolve_event(event_id: str) -> dict:
        """Get an event's file, line, namespace, and the file's other declared namespaces."""
        return resolve_event_tool(event_id, settings, event_index)

    @mcp.tool()
    def resolve_decision(decision_id: str) -> dict:
        """Get a decision's file, line, and category."""
        return resolve_decision_tool(decision_id, settings, decision_index)

    @mcp.tool()
    def resolve_idea(idea_id: str) -> dict:
        """Get an idea's file, line, category, and slot."""
        return resolve_idea_tool(idea_id, settings, idea_index)

    # ---------- parsers ----------

    @mcp.tool()
    def parse_file(
        path: str,
        max_bytes: int = 500_000,
        top_level_only: bool = False,
    ) -> dict:
        """Parse a .txt file to a JSON AST. Refuses files > max_bytes; top_level_only returns just top-level node names+lines."""
        return parse_file_tool(
            path,
            settings.mod_root,
            max_bytes=max_bytes,
            top_level_only=top_level_only,
        )

    @mcp.tool()
    def parse_string(text: str) -> dict:
        """Parse a snippet of paradox script to a JSON AST. Useful for validating generated blocks."""
        return parse_string_tool(text)

    # ---------- analysis ----------

    @mcp.tool()
    def find_focuses(
        tag: Optional[str] = None,
        has_prereq: Optional[str] = None,
        mutex_with: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 200,
    ) -> dict:
        """Search the focus index: by tag prefix, prereq, mutex partner, or kind. Returns id+file+line."""
        return find_focuses_tool(
            settings,
            focus_index,
            tag=tag,
            has_prereq=has_prereq,
            mutex_with=mutex_with,
            kind=kind,
            limit=limit,
        )

    @mcp.tool(name="find_references")
    def _find_references(
        kind: str,
        target: str,
        limit: int = 100,
        offset: int = 0,
        snippet_chars: int = 120,
        files_only: bool = False,
    ) -> dict:
        """Find every reference to a focus/event/decision/idea/loc/sprite/flag/variable. files_only collapses to a unique file list."""
        return find_references(
            settings.mod_root,
            kind,  # type: ignore[arg-type]
            target,
            vanilla_path=settings.vanilla_path,
            include_vanilla=False,
            limit=limit,
            offset=offset,
            snippet_chars=snippet_chars,
            files_only=files_only,
        )

    @mcp.tool(name="list_country_content")
    def _list_country_content(
        tag: str,
        include: Optional[list] = None,
        limit_per_category: int = 100,
    ) -> dict:
        """Files/IDs owned by a country TAG. Defaults to counts-only; pass include=['focuses',...] or include=['*'] for full lists."""
        return list_country_content(
            tag,
            settings.mod_root,
            focus_index=focus_index,
            event_index=event_index,
            decision_index=decision_index,
            idea_index=idea_index,
            loc_index=loc_index,
            include=include,
            limit_per_category=limit_per_category,
        )

    # ---------- validation ----------

    @mcp.tool()
    def validate(
        validator: Optional[str] = None,
        staged_only: bool = False,
        files: Optional[list] = None,
        strict: bool = False,
        severity_min: str = "info",
        limit: int = 500,
        counts_only: bool = False,
    ) -> dict:
        """Run a validator (or all fast ones). severity_min=info|warning|error filters; limit caps issues; counts_only skips the issues array."""
        return validate_tool(
            settings,
            validator_runner,
            validator=validator,
            staged_only=staged_only,
            files=files,
            strict=strict,
            severity_min=severity_min,
            limit=limit,
            counts_only=counts_only,
        )

    @mcp.tool()
    def validate_list() -> dict:
        """List available validators (name, title) for use with `validate`."""
        return validate_list_tool(settings)

    @mcp.tool()
    def lint(
        mode: str = "changed",
        files: Optional[list] = None,
        checks: Optional[list] = None,
        validators: Optional[list] = None,
        severity_min: str = "info",
        limit: int = 500,
        counts_only: bool = False,
    ) -> dict:
        """Run lint scripts plus style/brace checks for applicable script scopes. mode=changed|staged|all; checks=[...] subsets scripts; omit validators for scoped style, use [] to disable, or select ['auto'|'*'|names]; severity_min/limit/counts_only narrow output."""
        return lint_tool(
            settings.mod_root,
            mode=mode,
            files=files,
            checks=checks,
            validators=validators,
            severity_min=severity_min,
            limit=limit,
            counts_only=counts_only,
            validator_runner=validator_runner,
        )

    @mcp.tool()
    def review_branch(base: str = "main") -> dict:
        """Run review_branch.py: structured diff summary of the current branch vs `base`."""
        return review_branch_tool(settings.mod_root, base=base)

    # ---------- generators (M3) — return file content as strings ----------

    @mcp.tool(name="generate_focus")
    def _gen_focus(
        id: str,
        tag: str,
        x: int,
        y: int,
        cost: float = 10,
        icon: Optional[str] = None,
        relative_position_id: Optional[str] = None,
        prerequisites: Optional[list] = None,
        mutually_exclusive: Optional[list] = None,
        search_filters: Optional[list] = None,
        available: Optional[str] = None,
        completion_reward: Optional[str] = None,
        ai_base: int = 1,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> dict:
        """Scaffold a `focus = { ... }` block. Returns {txt, loc_yml_keys} — agent writes via Edit."""
        return generate_focus(
            id=id,
            tag=tag,
            x=x,
            y=y,
            cost=cost,
            icon=icon,
            relative_position_id=relative_position_id,
            prerequisites=prerequisites,
            mutually_exclusive=mutually_exclusive,
            search_filters=search_filters,
            available=available,
            completion_reward=completion_reward,
            ai_base=ai_base,
            title=title,
            description=description,
        )

    @mcp.tool(name="generate_event")
    def _gen_event(
        namespace: str,
        number: int,
        kind: str = "country_event",
        is_triggered_only: bool = True,
        fire_only_once: bool = False,
        picture: Optional[str] = None,
        trigger: Optional[str] = None,
        immediate: Optional[str] = None,
        options: Optional[list] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> dict:
        """Scaffold a country/news/state event block. Returns {txt, namespace_directive, loc_yml_keys}."""
        return generate_event(
            namespace=namespace,
            number=number,
            kind=kind,
            is_triggered_only=is_triggered_only,
            fire_only_once=fire_only_once,
            picture=picture,
            trigger=trigger,
            immediate=immediate,
            options=options,
            title=title,
            description=description,
        )

    @mcp.tool(name="generate_decision")
    def _gen_decision(
        id: str,
        tag: Optional[str] = None,
        icon: Optional[str] = None,
        cost: int = 25,
        days_remove: Optional[int] = None,
        days_re_enable: Optional[int] = None,
        allowed: Optional[str] = None,
        visible: Optional[str] = None,
        available: Optional[str] = None,
        complete_effect: Optional[str] = None,
        remove_effect: Optional[str] = None,
        cancel_trigger: Optional[str] = None,
        state_target: bool = False,
        target_root_trigger: Optional[str] = None,
        target_trigger: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> dict:
        """Scaffold a decision block. Goes inside an existing `<category> = { ... }`."""
        return generate_decision(
            id=id,
            tag=tag,
            icon=icon,
            cost=cost,
            days_remove=days_remove,
            days_re_enable=days_re_enable,
            allowed=allowed,
            visible=visible,
            available=available,
            complete_effect=complete_effect,
            remove_effect=remove_effect,
            cancel_trigger=cancel_trigger,
            state_target=state_target,
            target_root_trigger=target_root_trigger,
            target_trigger=target_trigger,
            title=title,
            description=description,
        )

    @mcp.tool(name="generate_idea")
    def _gen_idea(
        id: str,
        tag: Optional[str] = None,
        picture: Optional[str] = None,
        modifier: Optional[str] = None,
        research_bonus: Optional[str] = None,
        equipment_bonus: Optional[str] = None,
        targeted_modifier: Optional[str] = None,
        allowed: Optional[str] = None,
        available: Optional[str] = None,
        cancel: Optional[str] = None,
        cost: Optional[int] = None,
        removal_cost: int = -1,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> dict:
        """Scaffold an idea block. Goes inside `ideas = { <category> = { ... } }`."""
        return generate_idea(
            id=id,
            tag=tag,
            picture=picture,
            modifier=modifier,
            research_bonus=research_bonus,
            equipment_bonus=equipment_bonus,
            targeted_modifier=targeted_modifier,
            allowed=allowed,
            available=available,
            cancel=cancel,
            cost=cost,
            removal_cost=removal_cost,
            title=title,
            description=description,
        )

    @mcp.tool(name="generate_gfx_entry")
    def _gen_gfx(
        name: str,
        texturefile: str,
        kind: str = "spriteType",
        frames: Optional[int] = None,
        legacy_lazy_load: bool = False,
    ) -> dict:
        """Scaffold a `spriteType = { name = ... texturefile = ... }` entry. Goes inside `spriteTypes = { }`."""
        return generate_gfx_entry(
            name=name,
            texturefile=texturefile,
            kind=kind,
            frames=frames,
            legacy_lazy_load=legacy_lazy_load,
        )

    @mcp.tool(name="generate_gfx_merge")
    def _gen_gfx_merge(
        texture_dir: str,
        gfx_file: str,
        prefix: str = "GFX_",
        kind: str = "spriteType",
        frames: Optional[int] = None,
        legacy_lazy_load: bool = False,
        protected: Optional[list] = None,
        limit: int = 100,
        offset: int = 0,
        include_file: bool = False,
    ) -> dict:
        """Merge a texture dir into a .gfx file. Returns {txt} of new entries plus new/changed/orphaned; never writes. limit paginates names; include_file adds the full merged file."""
        return generate_gfx_merge(
            settings.mod_root,
            texture_dir=texture_dir,
            gfx_file=gfx_file,
            prefix=prefix,
            kind=kind,
            frames=frames,
            legacy_lazy_load=legacy_lazy_load,
            protected=protected,
            limit=limit,
            offset=offset,
            include_file=include_file,
        )

    @mcp.tool(name="generate_loc_stub")
    def _gen_loc(
        keys: list,
        lang: str = "l_english",
        include_header: bool = True,
        bom_prefix: bool = False,
    ) -> dict:
        """Build a localisation YAML stub from [{key, value}, ...]. Use bom_prefix=True for new files."""
        return generate_loc_stub(
            keys,
            lang=lang,
            include_header=include_header,
            bom_prefix=bom_prefix,
        )

    # ---------- M3 analysis ----------

    @mcp.tool(name="focus_graph")
    def _focus_graph(
        tag: str,
        detail: str = "summary",
        focus_ids: Optional[list] = None,
        node_limit: int = 100,
        edge_limit: int = 500,
        include_edges: bool = True,
        include_nodes: bool = True,
    ) -> dict:
        """Prereq/mutex DAG for a tag's focuses. detail=summary|ids|full|paths; paths (with focus_ids=[...], capped by node_limit) adds prereq chains with estimated days + ai_will_do. Default returns counts/roots/cycles/dangling only."""
        return focus_graph(
            tag,
            settings.mod_root,
            focus_index,
            vanilla_path=settings.vanilla_path,
            detail=detail,
            focus_ids=focus_ids,
            node_limit=node_limit,
            edge_limit=edge_limit,
            include_edges=include_edges,
            include_nodes=include_nodes,
        )

    @mcp.tool(name="check_refs")
    def _check_refs(
        tag: Optional[str] = None,
        files: Optional[list] = None,
        kinds: Optional[list] = None,
        limit: int = 200,
        offset: int = 0,
        counts_only: bool = False,
    ) -> dict:
        """Audit cross-references in a tag's focus files (or explicit files=): focus/event/idea/sprite/loc/decision ids resolved against the indexes; returns deduped unresolved refs with file:line sites. kinds=[...] subsets."""
        return check_refs(
            settings.mod_root,
            focus_index=focus_index,
            event_index=event_index,
            idea_index=idea_index,
            gfx_index=gfx_index,
            loc_index=loc_index,
            decision_index=decision_index,
            tag=tag,
            files=files,
            kinds=kinds,
            vanilla_path=settings.vanilla_path,
            lang=settings.default_lang,
            limit=limit,
            offset=offset,
            counts_only=counts_only,
        )

    @mcp.tool(name="focus_layout")
    def _focus_layout(
        tag: Optional[str] = None,
        file: Optional[str] = None,
        include_positions: bool = False,
        limit: int = 300,
    ) -> dict:
        """Focus tree geometry for a tag or file: resolves relative_position_id chains to absolute x/y; reports collisions, broken chains, bounding box. include_positions=True adds per-focus coordinates. limit caps each list."""
        return focus_layout(
            settings.mod_root,
            focus_index,
            tag=tag,
            file=file,
            vanilla_path=settings.vanilla_path,
            include_positions=include_positions,
            limit=limit,
        )

    @mcp.tool(name="diff_summary")
    def _diff_summary(
        base: str = "main",
        kinds: Optional[list] = None,
        with_ids: bool = True,
        limit: int = 200,
    ) -> dict:
        """Structured branch diff vs `base`. kinds filters by HOI4 kind; with_ids=False skips the per-file ID diff for speed."""
        return diff_summary(
            settings.mod_root,
            base=base,
            kinds=kinds,
            with_ids=with_ids,
            limit=limit,
        )

    @mcp.tool(name="check_encoding")
    def _check_encoding(files: Optional[list] = None) -> dict:
        """Verify BOM rules: .txt files must have no BOM, localisation/*.yml must have BOM."""
        return check_encoding(settings.mod_root, files=files)

    # ---------- resources ----------

    @mcp.resource("md://focus/{focus_id}")
    def focus_raw(focus_id: str) -> str:
        """Raw text of the `focus = { ... }` block, preserving original formatting."""
        return focus_resource(focus_id, settings, focus_index)

    @mcp.resource("md://loc/{key}")
    def loc_raw(key: str) -> str:
        """Localised value for the given key in the default language."""
        return loc_resource(key, settings, loc_index)

    @mcp.resource("md://sprite/{name}")
    def sprite_raw(name: str) -> str:
        """Raw text of the `spriteType = { ... }` block for the named sprite."""
        return sprite_resource(name, settings, gfx_index)

    @mcp.resource("md://event/{event_id}")
    def event_raw(event_id: str) -> str:
        """Raw text of the event block for `<namespace>.<n>`."""
        return event_resource(event_id, settings, event_index)

    @mcp.resource("md://decision/{decision_id}")
    def decision_raw(decision_id: str) -> str:
        """Raw text of the decision block."""
        return decision_resource(decision_id, settings, decision_index)

    @mcp.resource("md://idea/{idea_id}")
    def idea_raw(idea_id: str) -> str:
        """Raw text of the idea block."""
        return idea_resource(idea_id, settings, idea_index)

    return mcp


def main() -> None:  # pragma: no cover — entry point
    parser = argparse.ArgumentParser(prog="md-mcp", description="MCP server for Millennium Dawn")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="Start the MCP server (stdio transport)")
    p_serve.add_argument("--mod-root", help="Path to Millennium-Dawn checkout")
    p_serve.add_argument("--verbose", "-v", action="count", default=0)

    p_doctor = sub.add_parser("doctor", help="Print resolved configuration and exit")
    p_doctor.add_argument("--mod-root", help="Path to Millennium-Dawn checkout")

    p_index = sub.add_parser("build-index", help="Build all indexes and exit")
    p_index.add_argument("--mod-root", help="Path to Millennium-Dawn checkout")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", 0) > 1 else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = load(getattr(args, "mod_root", None))

    if args.cmd == "doctor":
        print(f"mod_root:       {settings.mod_root}")
        print(f"vanilla_path:   {settings.vanilla_path or '(not detected)'}")
        print(f"cache_dir:      {settings.cache_dir}")
        print(f"validator_mode: {settings.validator_mode}")
        print(f"default_lang:   {settings.default_lang}")
        sys.exit(0)

    if args.cmd == "build-index":
        for cls in (FocusIndex, LocalisationIndex, GfxIndex, EventIndex, DecisionIndex, IdeaIndex):
            idx = cls(settings.mod_root, settings.cache_dir, settings.vanilla_path)
            idx.ensure_fresh()
            keys = idx.list_keys()
            file_count = len(getattr(idx, "_by_file", {}))
            print(f"{cls.__name__:20s}  {len(keys):7d} keys  {file_count:4d} files")
        sys.exit(0)

    if args.cmd == "serve":
        # Forking from inside the MCP stdio loop deadlocks: child workers inherit
        # the parent's stdin/stdout FDs. Force serial parsing here. Users build
        # the cache up-front via `md-mcp build-index`; the server is meant for the
        # warm path (cache hit, ≈10 ms) and never needs the process pool.
        os.environ["MD_MCP_SERIAL_PARSE"] = "1"
        # Same reason, one layer out: most mod validators fork a Pool of their
        # own, so in-process validation hangs the server outright. It stays
        # available to the CLI and to library callers, just not under mcp.run().
        if settings.validator_mode != "isolated":
            logging.warning(
                "validator_mode=%s is unsafe under the stdio server (forking "
                "validators deadlock it); using isolated for this run",
                settings.validator_mode,
            )
            settings = replace(settings, validator_mode="isolated")
        mcp = build_server(settings)
        mcp.run()
