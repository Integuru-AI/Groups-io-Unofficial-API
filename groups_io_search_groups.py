from curl_cffi import requests
import re
import urllib.parse

BASE_URL = "https://groups.io"
APP_URL = "https://groups.io"


def run(headers, user_input):
    """Search groups.io for publicly listed groups matching a query, returning all pages of results."""
    base_url = BASE_URL

    query = user_input.get("query")
    if not query:
        return {'status_code': 400, 'body': {'error': 'query is required'}}

    max_pages = user_input.get("max_pages", 50)

    all_groups = []
    page = 1

    while page <= max_pages:
        params = {"q": query}
        if page > 1:
            params["page"] = str(page)

        url = f"{base_url}/search?" + urllib.parse.urlencode(params)

        response = requests.get(
            url,
            headers={**headers, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            impersonate="chrome131",
            timeout=30,
        )

        if response.status_code != 200:
            # Check for login redirect (session expired)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "")
                if "/login" in location:
                    return {'status_code': 401, 'body': {'error': 'Session expired'}}
            return {'status_code': response.status_code, 'body': {'error': f'Search request failed with status {response.status_code}'}}

        html = response.text

        # Check if we got redirected to login page
        if '<input' in html and 'name="password"' in html and '/login' in html:
            return {'status_code': 401, 'body': {'error': 'Session expired'}}

        # Extract total count
        total_match = re.search(r'(\d[\d,]*)\s*-\s*(\d[\d,]*)\s+of\s+(\d[\d,]*)', html)
        total_count = 0
        if total_match:
            total_count = int(total_match.group(3).replace(',', ''))

        # Parse groups from HTML
        groups = _parse_groups(html)
        if not groups:
            break

        all_groups.extend(groups)

        # Check if there are more pages
        if total_count <= page * 20:
            break

        page += 1

    return {
        'status_code': 200,
        'body': {
            'query': query,
            'total_results': total_count if total_count else len(all_groups),
            'groups': all_groups
        }
    }


def _parse_groups(html):
    """Parse group entries from search results HTML."""
    groups = []

    # Find the table body containing results
    # Each group is in a <tr><td> with an <h5> containing the link
    # Pattern: <a href="...">GroupName / <i>Display Name</i></a>
    # Then description text, then member/topic counts

    # Split by <tr> to get each group entry
    rows = re.split(r'<tr>\s*<td>', html)

    for row in rows[1:]:  # Skip first split which is before first <tr>
        # Stop at end of table
        if '</tbody>' in row:
            row = row[:row.index('</tbody>')]

        group = {}

        # Extract group URL and name
        link_match = re.search(r'<a href="(https?://[^"]+)">\s*(.*?)\s*</a>', row, re.DOTALL)
        if not link_match:
            continue

        group['url'] = link_match.group(1).strip()
        raw_name = link_match.group(2).strip()

        # Parse name - may contain "GroupName / <i>Display Name</i>"
        name_parts = re.sub(r'<[^>]+>', '', raw_name).strip()
        name_parts = re.sub(r'\s+', ' ', name_parts)
        group['name'] = name_parts

        # Extract description (text between </h5> and <br>)
        desc_match = re.search(r'</h5>\s*(.*?)\s*<br>', row, re.DOTALL)
        if desc_match:
            desc = desc_match.group(1).strip()
            desc = re.sub(r'<[^>]+>', '', desc)  # Remove HTML tags like <strong>
            desc = re.sub(r'\s+', ' ', desc).strip()
            group['description'] = desc

        # Extract member count
        members_match = re.search(r'([\d,]+)\s*Members?', row)
        if members_match:
            group['members'] = int(members_match.group(1).replace(',', ''))

        # Extract topic count
        topics_match = re.search(r'([\d,]+)\s*Topics?', row)
        if topics_match:
            group['topics'] = int(topics_match.group(1).replace(',', ''))

        # Extract archive type
        if 'Public Archive' in row:
            group['archive'] = 'Public'
        elif 'Private Archive' in row:
            group['archive'] = 'Private'
        else:
            group['archive'] = 'Unknown'

        # Extract if restricted
        group['restricted'] = 'Restricted' in row

        if group.get('url'):
            groups.append(group)

    return groups
