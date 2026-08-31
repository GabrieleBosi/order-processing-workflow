"""Command-line entry points.

    orderflow process <file>       run steps 1-4, show the trace
    orderflow process <file> --write --yes    also write to the ERP
    orderflow eval                 run the eval set (one command)
    orderflow serve                start the MVP web app
    orderflow erp                  list orders in the mock ERP
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config
from .models import RunStatus


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orderflow", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_process = sub.add_parser("process", help="Process one order document (steps 1-4).")
    p_process.add_argument("file", type=Path)
    p_process.add_argument("--write", action="store_true", help="Write to the ERP after confirmation.")
    p_process.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")
    p_process.add_argument("--json", action="store_true", help="Print the full run as JSON.")

    p_eval = sub.add_parser("eval", help="Run the eval set.")
    p_eval.add_argument("--cases", type=Path, default=None, help="Eval cases directory.")
    p_eval.add_argument("--judge", action="store_true", help="Also run the LLM judge (needs API key).")
    p_eval.add_argument("--report", type=Path, default=None, help="Where to write report.json.")
    p_eval.add_argument("--case", default=None, help="Run a single case by id.")

    p_serve = sub.add_parser("serve", help="Start the MVP web app.")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)

    sub.add_parser("erp", help="List orders currently in the mock ERP.")

    args = parser.parse_args(argv)
    if args.command == "process":
        return _cmd_process(args)
    if args.command == "eval":
        return _cmd_eval(args)
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "erp":
        return _cmd_erp()
    return 2


# ---------------------------------------------------------------- process


def _cmd_process(args) -> int:
    from .pipeline import Pipeline

    config = load_config()
    pipeline = Pipeline(config)
    if not config.llm_enabled():
        print("[i] No LLM credentials found: irregular text uses the deterministic heuristic extractor.")
    run = pipeline.process(args.file)

    print(f"\nRun {run.run_id} - {run.source_file}")
    for trace in run.traces:
        llm = f"  [LLM {trace.llm_usage.calls}x ${trace.llm_usage.cost_usd:.4f}]" if trace.llm_usage else ""
        status = "ok " if trace.status == "ok" else trace.status
        print(f"  {trace.step}. {trace.name:<10} {status:<6} {trace.duration_ms:7.0f} ms  "
              f"{trace.summary or trace.error or ''}{llm}")

    if run.status == RunStatus.FAILED:
        print(f"\nFAILED: {run.error}")
        return 1

    checked = run.checked
    print(f"\nOrder verdict: {checked.order_verdict.value}")
    for reason in checked.order_reasons:
        print(f"  - {reason}")
    print()
    for line in checked.lines:
        ext = line.reconciled.extracted
        product = line.reconciled.product.sku if line.reconciled.product else "?"
        mark = {"approve": "+", "review": "?", "reject": "x"}[line.verdict.value]
        qty = line.reconciled.quantity_t or ext.quantity or "?"
        price = ext.unit_price or line.reconciled.expected_price or "?"
        print(f"  [{mark}] line {ext.line_no}: {product}  {qty} t  @ {price}  -> {line.verdict.value}")
        for reason in line.reasons:
            print(f"        {reason}")

    if args.json:
        print(run.model_dump_json(indent=2))

    if run.status == RunStatus.REJECTED:
        print("\nOrder rejected: it cannot be written to the ERP.")
        print(f"Trace: {config.runs_dir / run.run_id}/")
        return 0

    if args.write:
        if not args.yes:
            reply = input("\nWrite this order to the ERP? [y/N] ").strip().lower()
            if reply not in ("y", "yes", "s", "si", "sì"):
                print("Not written. The run stays at awaiting_confirmation.")
                return 0
        run = pipeline.confirm(run)
        print(f"\nERP: {run.erp_result.message}")
    else:
        print("\nNot written to the ERP (use --write to confirm). Guardrail: writing requires a human yes.")
    print(f"Trace: {config.runs_dir / run.run_id}/")
    return 0


# ------------------------------------------------------------------- eval


def _cmd_eval(args) -> int:
    from .evals import run_evals

    config = load_config()
    report = run_evals(
        config,
        cases_dir=args.cases,
        judge=args.judge,
        only_case=args.case,
        report_path=args.report,
    )
    return 0 if report["failed_cases"] == 0 else 1


# ------------------------------------------------------------------ serve


def _cmd_serve(args) -> int:
    import uvicorn

    from .web import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


# -------------------------------------------------------------------- erp


def _cmd_erp() -> int:
    from .erp import MockERP

    config = load_config()
    erp = MockERP(config.erp_db_path)
    orders = erp.list_orders()
    if not orders:
        print("The mock ERP is empty.")
        return 0
    for order in orders:
        print(f"{order['erp_order_id']}  ref={order['order_ref'] or '-':<14} "
              f"{order['customer_name']:<28} {order['total_value']:>12,.2f} {order['currency']}  "
              f"({len(order['lines'])} lines, {order['created_at']})")
        for line in order["lines"]:
            print(f"    {line['line_no']}. {line['sku']:<14} {line['quantity_t'] or 0:>8.3f} t "
                  f"@ {line['unit_price'] or 0:>8.2f}  [{line['verdict']}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
