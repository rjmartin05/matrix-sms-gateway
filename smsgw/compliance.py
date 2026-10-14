"""Registered 10DLC message texts. Keep these byte-identical to the campaign registration.

STOP/START confirmations are sent by Telnyx (messaging profile autoresp_configs), never by the
bridge; their text lives here so the setup script configures Telnyx with the registered wording.
"""

from __future__ import annotations


def opt_in(brand: str) -> str:
    return (
        f"{brand}: You've joined the group text relay. Group messages will be sent to this number; "
        "reply to post to the group. Message frequency varies. Msg&data rates may apply. "
        "Reply HELP for help, STOP to opt out."
    )


def opt_in_followup(brand: str) -> str:
    # Sent as a separate message after the registered opt-in text so that text stays unmodified.
    return f"{brand}: To leave, text one word: STOP, CANCEL, END or QUIT. Text START to rejoin."


def opt_out(brand: str) -> str:
    return f"{brand}: You are unsubscribed and will receive no further messages. Reply START to rejoin."


def help_registered(brand: str, email: str) -> str:
    return f"{brand}: Group text relay. Reply STOP to opt out, START to rejoin. Help: {email}"


def not_allowlisted() -> str:
    return "Ask the group admin to add your number."
