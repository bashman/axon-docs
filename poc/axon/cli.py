"""axon CLI: run, build (AOT ELF), verify, serve (MCP), show, call, spec."""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

from .elf import aot_elf
from .frame import decode, encode
from .jit import jit_run
from .verify import DEFAULT_CAPS, Rejected, load_caps, verify

# The example spec shipped with the package (copied by `axon init`).
STARTER_SPEC = Path(__file__).resolve().parent.parent / "spec" / "contador-ejemplo.json"
HELLO_SPEC = Path(__file__).resolve().parent.parent / "spec" / "hello-rest.json"
HELLO_HANDLERS = Path(__file__).resolve().parent.parent / "spec" / "hello-rest.axon.txt"


def _caps(args):
    return load_caps(args.caps) if args.caps else DEFAULT_CAPS


def _frame_from_intent(path: str) -> bytes:
    with open(path) as f:
        return encode(json.load(f))


def _verified_nodes(frame: bytes, caps):
    nodes = decode(frame)
    verify(nodes, caps)
    return nodes


def cmd_run(args) -> int:
    nodes = _verified_nodes(_frame_from_intent(args.intent), _caps(args))
    jit_run(nodes)
    return 0


def cmd_build(args) -> int:
    frame = _frame_from_intent(args.intent)
    nodes = _verified_nodes(frame, _caps(args))
    seal = hashlib.sha256(frame).digest() if args.seal else None
    size = aot_elf(nodes, args.output, seal=seal)
    print(f"{args.output}: {size} bytes (frame {len(frame)} bytes)")
    return 0


def cmd_verify(args) -> int:
    with open(args.frame, "rb") as f:
        _verified_nodes(f.read(), _caps(args))
    print("OK")
    return 0


def cmd_serve(args) -> int:
    from .mcp_server import main as serve

    serve(
        args.db,
        args.spec,
        text_mode=not args.json,
        backend=args.backend,
        pg_dsn=args.pg_dsn,
    )
    return 0


def cmd_serve_http(args) -> int:
    from .http_host import serve_http

    serve_http(
        args.db,
        args.spec,
        args.port,
        host=args.host,
        test_mode=args.test_mode,
        backend=args.backend,
        pg_dsn=args.pg_dsn,
    )
    return 0


def _format_error(error: dict) -> str:
    """Use the enriched unknown-ref detail without changing other CLI errors."""
    detail = error.get("detail")
    if error.get("code") == "E_UNKNOWN_REF" and isinstance(detail, str) and detail:
        return f"error: {detail}"
    return json.dumps(error)


def cmd_call(args) -> int:
    from .service import Service
    from .store import Store

    raw_args = args.args if args.args is not None else "[]"
    try:
        parsed_args = json.loads(raw_args)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON arguments: {e}", file=sys.stderr)
        return 1

    if not isinstance(parsed_args, list):
        parsed_args = [parsed_args]

    store = Store(args.db)
    service = Service(store)
    out = service.run(args.ref, parsed_args)
    if not out.get("ok", True):
        err = out.get("error", {})
        print(_format_error(err), file=sys.stderr)
        return 1
    print(json.dumps(out))
    return 0


def cmd_spec(args) -> int:
    from .service import Service
    from .store import Store

    target_path = Path(args.target)
    store = Store(args.db)
    service = Service(store)

    if target_path.is_file():
        try:
            with open(target_path, encoding="utf-8") as f:
                spec_body = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"error: failed to read spec file '{target_path}': {e}", file=sys.stderr)
            return 1
        reg = service.register_spec(spec_body)
        if not reg.get("ok", True):
            print(json.dumps(reg["error"]), file=sys.stderr)
            return 1
        spec_name = reg.get("name") or spec_body.get("name")
    else:
        if target_path.suffix == ".json" and not target_path.exists():
            report = {
                "ok": False,
                "error": {
                    "node": args.target,
                    "code": "E_SPEC_NOT_FOUND",
                    "detail": f"spec file '{args.target}' not found",
                },
            }
            print(json.dumps(report), file=sys.stderr)
            return 1
        spec_name = args.target

    report = service.run_spec_compact(spec_name)
    print(json.dumps(report))
    if not report.get("ok", True):
        return 1
    if report.get("failed", 0) > 0 or report.get("failures"):
        return 1
    return 0


def cmd_define(args) -> int:
    """Register named functions from a text file (or stdin) through ``define_many``."""
    from .service import Service
    from .store import Store

    source_path = Path(args.src)
    try:
        if args.src == "-":
            text = sys.stdin.read()
        else:
            text = source_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"error: cannot read '{args.src}': {e}", file=sys.stderr)
        return 1

    store = Store(args.db)
    service = Service(store)
    if args.spec:
        spec_path = Path(args.spec)
        if spec_path.is_file():
            try:
                spec_body = json.loads(spec_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                print(f"error: failed to read spec file '{args.spec}': {e}", file=sys.stderr)
                return 1
            registered = service.register_spec(spec_body)
            if not registered.get("ok", True):
                print(json.dumps(registered["error"]), file=sys.stderr)
                return 1
        elif args.spec not in store.specs():
            if spec_path.suffix == ".json":
                print(
                    f"error: failed to read spec file '{args.spec}': file not found",
                    file=sys.stderr,
                )
            else:
                print(
                    f"error: spec '{args.spec}' is neither a file nor a registered spec name",
                    file=sys.stderr,
                )
            return 1

    out = service.define_many(src=text)
    if not out.get("ok", True):
        print(json.dumps(out), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "defined": [
                    {"name": entry["name"], "hash": entry["hash"]} for entry in out["defined"]
                ],
            }
        )
    )
    return 0


def cmd_show(args) -> int:
    from .service import Service
    from .store import Store

    service = Service(Store(args.db))
    names = args.names or service.root_names(limit=10_000)["names"]
    if not names:
        print("(the graph has no named roots yet; ask the agent to define one)", file=sys.stderr)
        return 0
    out = service.query_graph(names if len(names) > 1 else names[0], 1, "text")
    if not out.get("ok", True):
        err = out.get("error", {})
        print(_format_error(err), file=sys.stderr)
        return 1
    print(out["text"])
    return 0


def cmd_export(args) -> int:
    from .service import Service
    from .store import Store
    from .ts_export import export_ts

    store = Store(args.db)
    service = Service(store)
    code = export_ts(service)
    if args.out:
        out_path = Path(args.out)
        if out_path.suffix == ".ts":
            out_path.parent.mkdir(parents=True, exist_ok=True)
            target = out_path
        else:
            out_path.mkdir(parents=True, exist_ok=True)
            target = out_path / "index.ts"
        target.write_text(code, encoding="utf-8")
        print(f"Exported TypeScript module to {target}")
    else:
        print(code)
    return 0


def _mcp_add_lines(spec: Path, db: Path) -> list[str]:
    """The two one-line commands that wire a client to `axon serve` (absolute paths)."""
    argv = f"axon serve --db {db} --spec {spec}"
    return [f"codex mcp add axon -- {argv}", f"claude mcp add axon -- {argv}"]


def _json_error(error, source: str) -> str:
    """`JSON_FILE:LINE: message` for a JSON syntax error, `JSON_FILE: message` otherwise."""
    import json as _json

    if isinstance(error, _json.JSONDecodeError):
        return f"{source}:{error.lineno}: {error.msg}"
    return f"{source}: {error}"


def _save_failed_payload(target: Path, raw: str) -> Path:
    """Keep a failed `story add` payload next to FILE so the failure can be reproduced."""
    path = target.with_name(f".{target.name}.failed.json")
    try:
        path.write_text(raw, encoding="utf-8")
    except OSError:
        pass  # shortcut: the path is printed even when the save failed, the error matters more
    return path


def cmd_story_add(args) -> int:
    """`axon story add FILE --from-json JSON|-`: validate, then append one story block."""
    import sys as _sys

    from .stories.add import AddError, PayloadError, add_left_out, append_story, prepare_add

    if args.left_out:
        if args.from_json:
            print("usa «--left-out» o «--from-json», no los dos", file=sys.stderr)
            return 2
        try:
            for text in args.left_out:
                add_left_out(Path(args.file), text, dry_run=args.dry_run)
        except AddError as error:
            print(f"{args.file}: {error}", file=sys.stderr)
            return 1
        verb = "se añadiría" if args.dry_run else "añadido"
        print(f"{verb} a «Fuera de alcance»: {len(args.left_out)}")
        return 0
    if not args.from_json:
        print("falta «--from-json» o «--left-out»", file=sys.stderr)
        return 2
    source = args.from_json
    if source == "-":
        raw = _sys.stdin.read()
        source_name = "<stdin>"
    else:
        try:
            raw = Path(source).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            print(f"{source}: no se puede leer el JSON: {error}", file=_sys.stderr)
            return 1
        source_name = source
    target = Path(args.file)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        print(_json_error(error, source_name), file=_sys.stderr)
        if not args.dry_run:
            print(f"payload guardado en {_save_failed_payload(target, raw)}", file=_sys.stderr)
        return 1
    name = args.name or target.resolve().parent.name
    try:
        if args.dry_run:
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
            prepared = prepare_add(existing, data, name=name)
        else:
            prepared = append_story(target, data, name=name)
    except PayloadError as error:
        print(_json_error(error, source_name), file=_sys.stderr)
        if not args.dry_run:
            print(f"payload guardado en {_save_failed_payload(target, raw)}", file=_sys.stderr)
        return 1
    except AddError as error:
        if error.source == "write":
            print(f"{target}: {error}", file=_sys.stderr)
        elif error.source == "candidate":
            print(f"{target}: {error}", file=_sys.stderr)
        elif error.line is not None:
            print(f"{target}:{error.line}: {error}", file=_sys.stderr)
        else:
            print(f"{target}: {error}", file=_sys.stderr)
        if not args.dry_run:
            print(f"payload guardado en {_save_failed_payload(target, raw)}", file=_sys.stderr)
        return 1
    except (OSError, UnicodeDecodeError) as error:
        print(f"{target}: no se puede leer: {error}", file=_sys.stderr)
        if not args.dry_run:
            print(f"payload guardado en {_save_failed_payload(target, raw)}", file=_sys.stderr)
        return 1
    if args.dry_run:
        print(prepared.render, end="")
        return 0
    target.with_name(f".{target.name}.failed.json").unlink(missing_ok=True)
    ids = prepared.ids
    print(f"añadida {ids['story']} con casos {', '.join(ids['cases']) or '—'}")
    if ids["rules"]:
        print(f"reglas {', '.join(ids['rules'])}")
    print(prepared.render, end="")
    return 0


def cmd_story_approve(args) -> int:
    """`axon story approve FILE H-NN [--dry-run]`: approve one story, or show what would change."""
    from .stories.add import AddError, approve_story

    target = Path(args.file)
    name = args.name or target.resolve().parent.name
    try:
        result = approve_story(target, args.story_id, name=name, dry_run=args.dry_run)
    except AddError as error:
        if error.line is not None:
            print(f"{target}:{error.line}: {error}", file=sys.stderr)
        else:
            print(f"{target}: {error}", file=sys.stderr)
        return 1
    print(result.render, end="")
    print(result.message)
    return 0


def _story_cases(args, doc, name: str, *, english: bool = False) -> int:
    """`axon story cases FILE [-o OUT] [--name NAME]`: the approved cases as an HTTP-cases file."""
    from .http_cases import validate_cases_document
    from .stories import cases as story_cases
    from .stories.english import english_error
    from .stories.parse import StoryError

    try:
        document = story_cases.cases_doc(doc, name=name)
        validate_cases_document(document)
    except (StoryError, ValueError) as error:
        print(english_error(error) if english else error, file=sys.stderr)
        return 1
    body = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
        print(
            f"cases written to {args.out}" if english else f"casos escritos en {args.out}",
            file=sys.stderr,
        )
    else:
        print(body, end="")
    return 0


def cmd_story(args) -> int:
    """`axon story check|compile|cases|render|add|approve FILE`: the deterministic compiler."""
    if args.story_command == "add":
        return cmd_story_add(args)
    if args.story_command == "approve":
        return cmd_story_approve(args)
    from .stories.check import check_text
    from .stories.compile import compile_doc
    from .stories.english import english_error, is_english
    from .stories.parse import StoryError
    from .stories.render import render, render_technical

    try:
        text = Path(args.file).read_text(encoding="utf-8")
    except OSError as e:
        print(f"error: no se puede leer «{args.file}»: {e}", file=sys.stderr)
        return 1
    english = is_english(text)
    errors, doc = check_text(text)
    if doc is None or errors:
        for error in errors:
            print(english_error(error) if english else error, file=sys.stderr)
        return 1
    if args.story_command == "check":
        if english:
            print(f"ok: {args.file} ({len(doc.stories)} stories, {len(doc.rules)} rules)")
        else:
            print(f"ok: {args.file} ({len(doc.stories)} historias, {len(doc.rules)} reglas)")
        return 0
    if args.story_command == "render":
        view = render_technical(doc) if args.technical else render(doc)
        print(view, end="")
        return 0
    name = args.name or Path(args.file).resolve().parent.name
    if args.story_command == "cases":
        return _story_cases(args, doc, name, english=english)
    try:
        spec = compile_doc(doc, name=name)
    except StoryError as error:
        print(english_error(error) if english else error, file=sys.stderr)
        return 1
    body = json.dumps(spec, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
        print(
            f"spec written to {args.out}" if english else f"spec escrita en {args.out}",
            file=sys.stderr,
        )
    else:
        print(body, end="")
    return 0


def cmd_init(args) -> int:
    """Create a minimal project: empty graph, example spec, and client wiring hints."""
    from .store import Store

    target = Path(args.directory).expanduser().resolve()
    db = target / "axon.sqlite"
    spec = target / "spec.json"
    starter_spec = HELLO_SPEC if args.template == "hello" else STARTER_SPEC
    handlers = target / "handlers.axon.txt" if args.template == "hello" else None
    if db.exists() and not args.force:
        print(f"error: {db} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    if db.exists() and args.force:
        print(
            f"warning: overwriting {db} will delete the existing graph and all its history.",
            file=sys.stderr,
        )
        if not getattr(args, "yes", False):
            if sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
                try:
                    answer = input(f"Do you want to overwrite {db} and delete the graph? [y/N] ")
                except (EOFError, KeyboardInterrupt):
                    print("\naborted.", file=sys.stderr)
                    return 1
                if answer.strip().lower() not in ("y", "yes"):
                    print("aborted.", file=sys.stderr)
                    return 1
    sources = [starter_spec, *([HELLO_HANDLERS] if handlers else [])]
    if missing := next((source for source in sources if not source.is_file()), None):
        print(f"error: starter file not found: {missing}", file=sys.stderr)
        return 1
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(starter_spec, spec)
    if handlers:
        shutil.copyfile(HELLO_HANDLERS, handlers)
    if db.exists():
        db.unlink()
    Store(str(db))  # an empty graph, ready to serve (the spec is registered by `serve`)
    lines = _mcp_add_lines(spec, db)
    print(f"created {target}/")
    print("  spec.json     example spec (read-only for the agent)")
    if handlers:
        print("  handlers.axon.txt   example HTTP handlers")
    print("  axon.sqlite   empty graph")
    print()
    print("connect an agent (Codex or Claude Code):")
    for line in lines:
        print(f"  {line}")
    print()
    print("then read the graph with:  axon show --db " + str(db))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="axon")
    parser.add_argument("--caps", help="JSON file with granted capabilities")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="execute an intent file in memory (JIT)")
    run.add_argument("intent")
    run.set_defaults(func=cmd_run)

    build = sub.add_parser("build", help="freeze an intent file into a static ELF (AOT)")
    build.add_argument("intent")
    build.add_argument("-o", "--output", required=True)
    build.add_argument("--seal", action="store_true", help="embed the SHA-256 of the frame")
    build.set_defaults(func=cmd_build)

    ver = sub.add_parser("verify", help="verify a binary frame")
    ver.add_argument("frame")
    ver.set_defaults(func=cmd_verify)

    serve = sub.add_parser("serve", help="serve the graph to an agent over MCP (stdio)")
    serve.add_argument("--db", default="axon.sqlite", help="SQLite file (kept between sessions)")
    serve.add_argument("--spec", action="append", default=[], help="read-only spec (repeatable)")
    serve.add_argument("--json", action="store_true", help="JSON nodes instead of the text syntax")
    serve.add_argument("--backend", choices=("memory", "postgres"), default="memory")
    serve.add_argument("--pg-dsn", help="PostgreSQL connection string")
    serve.set_defaults(func=cmd_serve)

    http = sub.add_parser("serve-http", help="serve spec-declared JSON routes over HTTP")
    http.add_argument("--db", default="axon.sqlite", help="SQLite graph file")
    http.add_argument("--spec", required=True, help="read-only HTTP spec")
    http.add_argument("--port", type=int, required=True, help="TCP port")
    http.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    http.add_argument("--test-mode", action="store_true", help="enable injected time and reset")
    http.add_argument("--backend", choices=("memory", "postgres"), default="memory")
    http.add_argument("--pg-dsn", help="PostgreSQL connection string")
    http.set_defaults(func=cmd_serve_http)

    init = sub.add_parser("init", help="create a minimal project (empty graph + example spec)")
    init.add_argument("directory", help="directory to create (or fill)")
    init.add_argument(
        "--template", choices=("counter", "hello"), default="counter", help="starter project"
    )
    init.add_argument("--force", action="store_true", help="overwrite an existing axon.sqlite")
    init.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="confirm destructive actions without prompting",
    )
    init.set_defaults(func=cmd_init)

    call = sub.add_parser("call", help="execute a named root function with typed JSON arguments")
    call.add_argument("ref", help="root function name or hash")
    call.add_argument(
        "args",
        nargs="?",
        default="[]",
        help="JSON array of arguments, e.g. '[2, 3]' (default: '[]')",
    )
    call.add_argument("--db", default="axon.sqlite", help="SQLite file (default: axon.sqlite)")
    call.set_defaults(func=cmd_call)

    spec_cmd = sub.add_parser("spec", help="register/run a spec and print compact report")
    spec_cmd.add_argument("target", help="spec name or path to spec JSON file")
    spec_cmd.add_argument("--db", default="axon.sqlite", help="SQLite file (default: axon.sqlite)")
    spec_cmd.set_defaults(func=cmd_spec)

    define = sub.add_parser("define", help="register functions from textual IR (S-expressions)")
    define.add_argument(
        "--src",
        required=True,
        help="file with one `(fn name ...)` per function; '-' reads stdin",
    )
    define.add_argument("--db", default="axon.sqlite", help="SQLite file (default: axon.sqlite)")
    define.add_argument("--spec", help="optional spec JSON to register before defining (read-only)")
    define.set_defaults(func=cmd_define)

    show = sub.add_parser("show", help="print the program in the graph as readable text")
    show.add_argument("names", nargs="*", help="function names (default: every root)")
    show.add_argument("--db", default="axon.sqlite")
    show.set_defaults(func=cmd_show)

    exp = sub.add_parser("export", help="export the graph to a TypeScript module")
    exp.add_argument("--db", default="axon.sqlite", help="SQLite file (kept between sessions)")
    exp.add_argument("--out", help="output directory or file (default: stdout)")
    exp.set_defaults(func=cmd_export)

    story = sub.add_parser("story", help="check, compile, render or add to a historias.md")
    story_sub = story.add_subparsers(dest="story_command", required=True)
    for name, help_text in (
        ("check", "validate the stories (grammar, ids, rules)"),
        ("compile", "compile the stories to a spec JSON"),
        ("cases", "export the approved cases as an HTTP-cases JSON"),
        ("render", "print the prose review with the SUPUESTO list"),
    ):
        sub_cmd = story_sub.add_parser(name, help=help_text)
        sub_cmd.add_argument("file", help="path to historias.md")
        if name in ("compile", "cases"):
            sub_cmd.add_argument("-o", "--out", help="output JSON file (default: stdout)")
        if name in ("compile", "cases", "check", "render"):
            sub_cmd.add_argument("--name", help="project name (default: the directory of the file)")
        if name == "render":
            sub_cmd.add_argument(
                "--technical",
                action="store_true",
                help="show the technical view (routes, JSON bodies, status codes)",
            )
    add = story_sub.add_parser(
        "add", help="append one story from a JSON payload, or one «fuera» note"
    )
    add.add_argument("file", help="path to historias.md (created if missing)")
    add.add_argument("--from-json", help="JSON payload file; '-' reads UTF-8 JSON from stdin")
    add.add_argument(
        "--left-out",
        action="append",
        metavar="TEXT",
        help="add «FUERA: TEXT.» to the «Fuera de alcance» section (no story needed)",
    )
    add.add_argument("--name", help="spec name (default: the directory of the file)")
    add.add_argument(
        "--dry-run", action="store_true", help="validate and print the review, write nothing"
    )
    approve = story_sub.add_parser(
        "approve", help="approve one «propuesta»/«pospuesta» story (only its Estado: line)"
    )
    approve.add_argument("file", help="path to historias.md")
    approve.add_argument("story_id", metavar="H-NN", help="the story to approve, e.g. H-02")
    approve.add_argument("--name", help="spec name (default: the directory of the file)")
    approve.add_argument(
        "--dry-run",
        action="store_true",
        help="check, compile and print the review, write nothing (also resumes a paused story)",
    )
    story.set_defaults(func=cmd_story)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Rejected as e:
        print(json.dumps(e.info), file=sys.stderr)
        return 2
    except (ValueError, OSError, KeyError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
