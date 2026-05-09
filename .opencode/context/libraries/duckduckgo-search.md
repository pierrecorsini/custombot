### DDGS text() Method Usage Examples

Source: https://github.com/deedy5/duckduckgo_search/blob/main/README.md

Provides examples of using the `DDGS().text()` method for various search queries, including a general search and a search for specific file types (PDFs). It also shows the expected structure of the returned search results.

```python
results = DDGS().text('live free or die', region='wt-wt', safesearch='off', timelimit='y', max_results=10)
# Searching for pdf files
results = DDGS().text('russia filetype:pdf', region='wt-wt', safesearch='off', timelimit='y', max_results=10)
print(results)
[
    {
        "title": "News, sport, celebrities and gossip | The Sun",
        "href": "https://www.thesun.co.uk/",
        "body": "Get the latest news, exclusives, sport, celebrities, showbiz, politics, business and lifestyle from The Sun"
    }, ...
]
```

--------------------------------

### Initialize DDGS Class and Perform Text Search

Source: https://github.com/deedy5/duckduckgo_search/blob/main/README.md

Demonstrates how to import and initialize the `DDGS` class, then perform a basic text search using the `text()` method with a `max_results` limit. The results are printed to the console.

```python3
from duckduckgo_search import DDGS

results = DDGS().text("python programming", max_results=5)
print(results)
```

--------------------------------

### DDGS text() Method Definition

Source: https://github.com/deedy5/duckduckgo_search/blob/main/README.md

Defines the `text()` method of the `DDGS` class, used for performing text searches on DuckDuckGo. It accepts keywords, region, safesearch settings, time limits, backend, and a maximum number of results. It returns a list of dictionaries containing search results.

```APIDOC
text(keywords: str, region: str = "wt-wt", safesearch: str = "moderate", timelimit: str | None = None, backend: str = "auto", max_results: int | None = None) -> list[dict[str, str]]:
  keywords: keywords for query.
  region: wt-wt, us-en, uk-en, ru-ru, etc. Defaults to "wt-wt".
  safesearch: on, moderate, off. Defaults to "moderate".
  timelimit: d, w, m, y. Defaults to None.
  backend: auto, html, lite. Defaults to auto.
    auto - try all backends in random order,
    html - collect data from https://html.duckduckgo.com,
    lite - collect data from https://lite.duckduckgo.com.
  max_results: max number of results. If None, returns results only from the first response. Defaults to None.
```

--------------------------------

### Configure DDGS with Tor Browser Proxy

Source: https://github.com/deedy5/duckduckgo_search/blob/main/README.md

Shows how to configure the `DDGS` class to use the Tor Browser's SOCKS5 proxy, aliased as 'tb', for anonymous search requests. It sets a timeout and performs a text search.

```python3
ddgs = DDGS(proxy="tb", timeout=20)  # "tb" is an alias for "socks5://127.0.0.1:9150"
results = ddgs.text("something you need", max_results=50)
```

--------------------------------

### Duckduckgo_search CLI Usage Examples

Source: https://github.com/deedy5/duckduckgo_search/blob/main/README.md

Demonstrates various command-line interface (CLI) commands for `ddgs`, including text search, downloading files, using proxies (Tor), saving results to CSV, disabling SSL verification, image search, and news search with output to JSON.

```Python3
# text search
ddgs text -k "Assyrian siege of Jerusalem"
# find and download pdf files via proxy
ddgs text -k "Economics in one lesson filetype:pdf" -r wt-wt -m 50 -p https://1.2.3.4:1234 -d -dd economics_reading
# using Tor Browser as a proxy (`tb` is an alias for `socks5://127.0.0.1:9150`)
ddgs text -k "'The history of the Standard Oil Company' filetype:doc" -m 50 -d -p tb
# find and save to csv
ddgs text -k "'neuroscience exploring the brain' filetype:pdf" -m 70 -o neuroscience_list.csv
# don't verify SSL when making the request
ddgs text -k "Mississippi Burning" -v false
# find and download images
ddgs images -k "beware of false prophets" -r wt-wt -type photo -m 500 -d
# get news for the last day and save to json
ddgs news -k "sanctions" -m 100 -t d -o json
```