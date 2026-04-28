# 1. Record architecture decisions

Date: 2025-02-08

## Status

Accepted

## Context

This project makes a few non-obvious statistical choices that a reader will
reasonably want explained. The reasoning matters more than the code, because the
code is a direct consequence of the reasoning.

## Decision

Keep a short architecture decision record for each significant choice, using the
format popularized by Michael Nygard. One file per decision, numbered, never
rewritten once accepted (superseded instead).

## Consequences

A reviewer can read the ADRs and understand why the engine gates on confidence
intervals rather than point estimates, without reverse-engineering it from the
code. New contributors get the same context.
