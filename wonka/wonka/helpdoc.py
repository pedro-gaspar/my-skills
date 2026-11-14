"""wonka help: a reference of every subcommand, generated from the argparse parser so it never drifts."""

import argparse

from .store import WonkaError


def _commands(p: argparse.ArgumentParser) -> argparse._SubParsersAction:
    return next(a for a in p._actions if a.dest == "cmd")


def block(p: argparse.ArgumentParser, name: str) -> str:
    sub = _commands(p)
    summary = next(a.help for a in sub._choices_actions if a.dest == name)
    return f"## wonka {name}\n{summary}\n\n{sub.choices[name].format_help()}"


def reference(p: argparse.ArgumentParser, name: str | None = None) -> str:
    choices = _commands(p).choices
    if name is not None:
        if name not in choices:
            raise WonkaError(f"unknown command {name!r}; use one of {', '.join(choices)}")
        return block(p, name)
    head = f"🎩 wonka: every command\n{p.description}\nwonka help <command> shows one\n"
    return "\n\n".join([head, *(block(p, n) for n in choices)])
