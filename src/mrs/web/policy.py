"""HTTP policy shared by route registration, clients and regression checks."""
from __future__ import annotations

import functools
import inspect
from typing import get_type_hints

from fastapi import Body, Depends, Request
from fastapi.params import Query
from fastapi.routing import APIRoute
from starlette.routing import Match

# The lists are intentionally exhaustive: an API route without an explicit
# authorization decision fails while the app is imported, not when it is
# first found by a guest. OWNER routes must also use require_admin in their
# actual FastAPI dependency graph; this table cannot grant authority by itself.
OWNER = set('''backup restore unskip health sessions restart audio audio/devices
audio/device setup/state setup/tools setup/done token passes passes/new profiles
passes/extend passes/revoke lockdown port/shuffle blocked ddns network qr cache
theme announce sleep download pin lockips groqkey groqmodels groqmodel boot
boot/status boot/early cookies cookies/find cookies/extension cookies/signedin
cookies/import cookies/grab openfolder library/scan library/paths lastfm alarms
cast diag audit policy stats accounts accounts/scope accounts/forget accounts/check
stream/{video_id}
autoeq/status autoeq/assign'''.split())
SCOPED = set('''status play play/video/{video_id} control/{action} session/ended
session/progress session/here seek queue/{op} cancel radio search play/artist
play/album lyrics lyrics/search about history block blocks history/forget liked
playlists station foryou spectrum spotify/add playlist/{op} settings setting
whoami output/stream/{video_id} output/prepare/{video_id} output/stats
announce/{aid}.mp3 source smartplaylists smartplaylists/play autoeq/search
autoeq/profile autoeq/match
me me/consent me/rename me/export me/forget-taste me/delete
me/siri me/siri/new me/siri/token me/siri/revoke'''.split())
READ_ONLY = set('''ping status health history
blocks liked playlists settings audio/devices setup/state whoami passes profiles
boot/status cookies diag output/stats audit policy
output/stream/{video_id} announce/{aid}.mp3 stream/{video_id} events
smartplaylists
autoeq/status stats accounts accounts/check me me/export me/siri'''.split())
READ_PARAMS = {
    "sessions": {"close"}, "cache": {"prune"}, "blocked": {"forgive", "clear"},
    "ddns": {"hostname", "user", "secret", "provider", "now"},
    "library/paths": {"add", "remove"}, "lastfm": {"step", "api_key", "secret"},
    "alarms": {"add", "remove"}, "cast": {"add", "remove", "text"},
    "audio": {"eq", "normalize", "crossfade"},
}


def changing(path: str, params, path_params=None) -> bool:
    """Whether a GET request for this route can cause externally visible change."""
    key = path.removeprefix("/api/")
    if key in READ_ONLY:
        return False
    if key in READ_PARAMS:
        return bool(set(params) & READ_PARAMS[key])
    if key == "playlist/{op}":
        return (path_params or {}).get("op") != "tracks"
    return True


def matching_route(app, scope):
    """Find the concrete API route and its extracted path parameters."""
    for route in app.router.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        matched, child_scope = route.matches(scope)
        if matched is Match.FULL:
            return route, child_scope
    return None, None


def install(app, admin):
    """Register JSON POST variants for API handlers, preserving their guards.

    Existing handlers remain the single implementation. Their ordinary query
    parameters become fields in an embedded JSON object, while path parameters,
    Request injection and Depends guards remain FastAPI-managed.
    """
    originals = [r for r in app.routes
                 if isinstance(r, APIRoute) and r.path.startswith("/api/")]
    seen: set[str] = set()
    for route in originals:
        key = route.path[5:]
        if key not in OWNER | SCOPED | {"ping", "events"}:
            raise RuntimeError(f"No authorization policy for {route.path}")
        if key in OWNER:
            guards = {d.call for d in route.dependant.dependencies}
            if admin not in guards:
                raise RuntimeError(f"Owner route lacks owner guard: {route.path}")
        if (key == "events" or "stream/" in key or key.startswith("announce/")):
            continue
        if route.path in seen:
            continue
        seen.add(route.path)

        endpoint = route.endpoint
        sig = inspect.signature(endpoint)
        hints = get_type_hints(endpoint)
        parameters = []
        for name, param in sig.parameters.items():
            annotation = hints.get(name, param.annotation)
            default = param.default
            if (name not in route.param_convertors and name != "_"
                    and annotation is not Request):
                alias = name
                if isinstance(default, Query):
                    alias = default.alias or name
                    default = default.default
                if default is inspect.Parameter.empty:
                    default = ...
                default = Body(default=default, embed=True, alias=alias)
            parameters.append(param.replace(annotation=annotation, default=default))

        def make_wrapper(fn):
            @functools.wraps(fn)
            def wrapped(**kwargs):
                return fn(**kwargs)
            return wrapped

        def make_async_wrapper(fn):
            @functools.wraps(fn)
            async def wrapped(**kwargs):
                return await fn(**kwargs)
            return wrapped

        wrapper = (make_async_wrapper(endpoint)
                   if inspect.iscoroutinefunction(endpoint)
                   else make_wrapper(endpoint))
        wrapper.__signature__ = sig.replace(
            parameters=parameters,
            return_annotation=hints.get("return", sig.return_annotation))
        app.add_api_route(
            route.path, wrapper, methods=["POST"], name=f"{route.name}_post",
            response_class=route.response_class, status_code=route.status_code,
            dependencies=route.dependencies, responses=route.responses,
            include_in_schema=route.include_in_schema,
        )
