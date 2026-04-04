"""Test search endpoints using cookies from .env (same as saved_posts_outreach.py)."""
import requests, json, re, os
from dotenv import load_dotenv
load_dotenv()

li_at = os.getenv("LINKEDIN_LI_AT", "")
jsessionid = os.getenv("LINKEDIN_JSESSIONID", "").strip('"')

if not li_at or not jsessionid:
    print("ERROR: Set LINKEDIN_LI_AT and LINKEDIN_JSESSIONID in .env")
    exit(1)

print(f"li_at: ...{li_at[-20:]}")
print(f"jsessionid: {jsessionid}")

s = requests.Session()
s.cookies.set("li_at", li_at, domain=".linkedin.com")
s.cookies.set("JSESSIONID", f'"{jsessionid}"', domain=".linkedin.com")
s.headers.update({
    "Accept": "application/vnd.linkedin.normalized+json+2.1",
    "x-restli-protocol-version": "2.0.0",
    "csrf-token": jsessionid,
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
})

urls = {
    "1_saved_posts (CONTROL)":
        "https://www.linkedin.com/voyager/api/graphql"
        "?variables=(start:0,query:(flagshipSearchIntent:SEARCH_MY_ITEMS_SAVED_POSTS))"
        "&queryId=voyagerSearchDashClusters.05111e1b90ee7fea15bebe9f9410ced9",

    "2_graphql_search":
        "https://www.linkedin.com/voyager/api/graphql"
        "?variables=(start:0,query:(keywords:ai%20contract%20hiring,"
        "flagshipSearchIntent:SEARCH_SRP,"
        "queryParameters:List("
        "(key:resultType,value:List(CONTENT)),"
        "(key:sortBy,value:List(date_posted)),"
        "(key:datePosted,value:List(past-24h)))))"
        "&queryId=voyagerSearchDashClusters.05111e1b90ee7fea15bebe9f9410ced9",

    "3_blended":
        "https://www.linkedin.com/voyager/api/search/blended"
        "?count=10&keywords=ai+contract+hiring&origin=FACETED_SEARCH"
        "&q=all&filters=List(resultType->CONTENT,sortBy->DATE_POSTED,datePosted->past-24h)&start=0",

    "4_dash_clusters":
        "https://www.linkedin.com/voyager/api/search/dash/clusters"
        "?decorationId=com.linkedin.voyager.dash.deco.search.SearchClusterCollection-180"
        "&origin=FACETED_SEARCH&q=all"
        "&query=(keywords:ai%20contract%20hiring,queryParameters:List("
        "(key:resultType,value:List(CONTENT)),"
        "(key:sortBy,value:List(date_posted)),"
        "(key:datePosted,value:List(past-24h))))"
        "&start=0&count=10",
}

print(f"\nTesting {len(urls)} endpoints...\n")

for name, url in urls.items():
    try:
        r = s.get(url, timeout=15, allow_redirects=False)
        status = r.status_code

        if status in (301, 302, 303, 307, 308):
            print(f"  {name}: {status} REDIRECT")
        elif status == 200:
            data = r.json()
            urns = set(re.findall(r"urn:li:activity:\d+", json.dumps(data)))
            print(f"  {name}: 200 OK, {len(r.content)} bytes, {len(urns)} activity URNs")
            json.dump(data, open(f"test_{name.split('_')[0]}.json", "w"), indent=2)
            print(f"    -> Saved to test_{name.split('_')[0]}.json")
        else:
            print(f"  {name}: {status}, {len(r.content)} bytes")
    except Exception as e:
        print(f"  {name}: ERROR - {e}")