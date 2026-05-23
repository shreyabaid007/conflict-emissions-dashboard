"""WCED operations CLI.

Entrypoint registered as ``wced`` via pyproject ``[project.scripts]``.
Each subcommand is a thin wrapper around library functions; the CLI itself
contains no business logic — that lives in ``wced.quantify``, ``wced.detect``,
etc.

Current subcommands:

- ``wced factors list``         — print every loaded emission factor.
- ``wced factors show <key>``   — pretty-print a single factor for review.
- ``wced parameters list``      — print every Monte Carlo parameter prior.
- ``wced parameters show <key>``— pretty-print a single parameter prior.
- ``wced verify pending``       — list events awaiting editorial review.
- ``wced verify show <id>``     — show event details and editorial history.
- ``wced verify approve <id>``  — approve and publish an event.
- ``wced verify reject <id>``   — reject an event with a reason.
- ``wced verify retract <id>``  — retract a published event (public changelog).
"""
from __future__ import annotations

import json
from pathlib import Path

import typer

from wced.cli.verify import app as verify_app
from wced.quantify.factors import (
    EmissionFactor,
    FactorRegistry,
    load_factors,
    load_parameter_distributions,
)

app = typer.Typer(
    help="WCED operations CLI.",
    no_args_is_help=True,
    add_completion=False,
)

factors_app = typer.Typer(
    help="Inspect emission factors loaded from data/emission_factors.yaml.",
    no_args_is_help=True,
)
parameters_app = typer.Typer(
    help="Inspect Monte Carlo priors loaded from data/parameter_distributions.yaml.",
    no_args_is_help=True,
)
app.add_typer(factors_app, name="factors")
app.add_typer(parameters_app, name="parameters")
app.add_typer(verify_app, name="verify")


def _format_factor_line(f: EmissionFactor) -> str:
    """One-line summary used by both ``list`` subcommands."""
    if f.distribution == "normal":
        params = f"mean={f.value}, sigma={f.sigma}"
    elif f.distribution in ("triangular",):
        params = f"low={f.low}, mode={f.mode}, high={f.high}"
    elif f.distribution == "uniform":
        params = f"low={f.low}, high={f.high}"
    else:  # constant
        params = f"value={f.value}"
    return f"{f.key}  [{f.distribution}]  {params}  ({f.units})  §{f.methodology_section}"


def _format_factor_full(f: EmissionFactor) -> str:
    """Pretty-printed JSON used by both ``show`` subcommands."""
    return json.dumps(f.model_dump(mode="json"), indent=2, sort_keys=True)


def _list_registry(registry: FactorRegistry) -> None:
    typer.echo(f"# Source: {registry.source_path}")
    for key in registry.keys():
        typer.echo(_format_factor_line(registry[key]))


def _show_one(registry: FactorRegistry, key: str) -> None:
    if key not in registry:
        typer.echo(
            f"Unknown key {key!r}. Known: {', '.join(registry.keys())}",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(_format_factor_full(registry[key]))


# ---------------------------------------------------------------------------
# factors
# ---------------------------------------------------------------------------


@factors_app.command("list")
def factors_list(
    path: Path | None = typer.Option(
        None,
        "--path",
        help="Override path to the emission factors YAML.",
    ),
) -> None:
    """Print every emission factor loaded from the YAML file."""
    _list_registry(load_factors(path))


@factors_app.command("show")
def factors_show(
    key: str = typer.Argument(..., help="Factor key, e.g. crude_oil_combustion."),
    path: Path | None = typer.Option(
        None,
        "--path",
        help="Override path to the emission factors YAML.",
    ),
) -> None:
    """Pretty-print one emission factor as JSON."""
    _show_one(load_factors(path), key)


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------


@parameters_app.command("list")
def parameters_list(
    path: Path | None = typer.Option(
        None,
        "--path",
        help="Override path to the parameter distributions YAML.",
    ),
) -> None:
    """Print every parameter prior loaded from the YAML file."""
    _list_registry(load_parameter_distributions(path))


@parameters_app.command("show")
def parameters_show(
    key: str = typer.Argument(..., help="Parameter key, e.g. burn_duty_cycle."),
    path: Path | None = typer.Option(
        None,
        "--path",
        help="Override path to the parameter distributions YAML.",
    ),
) -> None:
    """Pretty-print one parameter prior as JSON."""
    _show_one(load_parameter_distributions(path), key)


if __name__ == "__main__":  # pragma: no cover
    app()
