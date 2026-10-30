feed_sample.json is a trimmed, live-derived sample from one request to the
Fireship feed (HTTP 200 on 2026-10-30). It contains two parsed entries and
selected feedparser fields. The successful live feed used Atom 1.0; entry fields
included yt_videoid, title, author, published, summary, media_thumbnail,
media_content, media_statistics, and media_starrating. Feed fields included
author, title, id, link, and yt_channelid.

synthetic_feed.xml is a minimal synthetic Atom fixture used for parser tests.
The other five configured channels were not live-probed.
