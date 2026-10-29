These fixtures are trimmed, live-derived Hacker News API responses fetched on
2026-10-30 from https://hacker-news.firebaseio.com/v0.

topstories.json and beststories.json contain the first 10 IDs from each live
list. items.json contains the first three live top-story item payloads. The
sampled item fields included by, descendants, id, kids, score, text, time,
title, type, and url; text was absent from one sampled item.

The live endpoints were reachable during capture; these are not synthetic
fixtures. Tests use the stored responses and mocked HTTP only.
