reddit_hot.json.status.txt records the live hot.json response: HTTP 403 with
text/html content. It was not retried during the live shape check, and no
workaround was attempted. Reddit JSON field names therefore could not be
verified live; synthetic_hot.json follows the documented Listing/data/children
shape and is explicitly synthetic.

rss_sample.json retains the parsed Atom/feedparser structure and selected
fields from a live-derived LocalLLaMA hot RSS sample (HTTP 200 on 2026-10-30).
Its author values are replaced with synthetic usernames and the media URL's
signature-like query value was removed. It is sanitized/redacted sample
content, not an untouched live capture. Feed and entry fields remain in the
shape consumed by the parser.

synthetic_hot.rss is a minimal synthetic Atom fixture used for fallback parser
tests. synthetic_hot.json is also synthetic. The live JSON refusal and RSS
success were each requested exactly once.
