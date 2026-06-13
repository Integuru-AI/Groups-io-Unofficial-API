from curl_cffi import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import time
import datetime
import html as html_lib

BASE_URL = "https://groups.io"
APP_URL = "https://groups.io"

CONCURRENCY = 10
MAX_RETRIES = 2
TIME_BUDGET_SECONDS = 8  # Return results before gateway timeout (~10-15s)

DEFAULT_ICP_GROUPS = [
    # Tier 1 — Direct pretreatment / water utility (33,600+ messages)
    "Pretreatment",
    # Tier 2 — Watershed / environmental / water adjacent
    "AWCAC",                   # 5,908 messages
    "FriendsoftheWoodlands",   # 551 messages
    "CSA7",                    # 137 messages
    "traversegreen",           # 94 messages
    "MoCoWatershed",           # 87 messages
    "SDVolunteerMonitoring",   # 28 messages
    "RainWaterHarvesting",     # 7 messages
    "midwestpretreatment",     # 4 messages
    "stormwater",              # 1 message
]


def run(headers, user_input):
    """Harvest contact signatures from messages across targeted groups.io groups, deduplicated by poster."""
    base_url = BASE_URL

    groups = user_input.get("groups", DEFAULT_ICP_GROUPS)
    max_pages = user_input.get("max_pages_per_group", 10)
    start_page = user_input.get("start_page", 1)
    start_group = user_input.get("start_group", 0)

    contacts = {}  # poster_id -> contact dict
    total_messages = 0
    groups_scanned = []
    groups_completed = []
    errors = []
    start_time = time.time()
    timed_out = False
    last_page_fetched = start_page
    timed_out_group = None

    for group_idx, group_name in enumerate(groups):
        if group_idx < start_group:
            continue
        if timed_out:
            break

        group_msgs = 0
        # First group uses start_page (for resume); subsequent groups start at 1
        first_page = start_page if group_idx == start_group else 1
        last_page_fetched = first_page

        # Phase 1: fetch first page to discover total message count
        page_html, err = _fetch_expanded_page(base_url, group_name, first_page, headers)

        if err == "auth":
            return {'status_code': 401, 'body': {'error': 'Session expired'}}
        if err:
            errors.append({"group": group_name, "page": first_page, "error": err})
            continue

        total_match = re.search(r'(\d[\d,]*)\s*-\s*(\d[\d,]*)\s+of\s+(\d[\d,]*)', page_html)
        group_total = int(total_match.group(3).replace(',', '')) if total_match else 0

        messages = _parse_expanded_messages(page_html)
        if messages:
            group_msgs += _merge_messages(messages, contacts, group_name)
            total_messages += len(messages)

        end_page = min((group_total + 19) // 20, first_page + max_pages - 1)

        # Phase 2: fetch remaining pages concurrently in batches
        if end_page > first_page:
            remaining = list(range(first_page + 1, end_page + 1))
            auth_failed = False
            batch_size = CONCURRENCY * 5  # 50 pages per batch

            for batch_start in range(0, len(remaining), batch_size):
                if auth_failed:
                    break
                # Check time budget before starting next batch
                if time.time() - start_time >= TIME_BUDGET_SECONDS:
                    timed_out = True
                    timed_out_group = group_idx
                    break

                batch = remaining[batch_start:batch_start + batch_size]

                with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
                    futures = {
                        executor.submit(_fetch_expanded_page, base_url, group_name, p, headers): p
                        for p in batch
                    }
                    for future in as_completed(futures):
                        page_num = futures[future]
                        try:
                            pg_html, pg_err = future.result()
                        except Exception as e:
                            errors.append({"group": group_name, "page": page_num, "error": str(e)})
                            continue

                        if pg_err == "auth":
                            auth_failed = True
                            break
                        if pg_err:
                            errors.append({"group": group_name, "page": page_num, "error": pg_err})
                            continue
                        if not pg_html:
                            continue

                        msgs = _parse_expanded_messages(pg_html)
                        if msgs:
                            group_msgs += _merge_messages(msgs, contacts, group_name)
                            total_messages += len(msgs)
                        if page_num > last_page_fetched:
                            last_page_fetched = page_num

                # Brief pause between batches to avoid rate limiting
                if batch_start + batch_size < len(remaining) and not timed_out:
                    time.sleep(0.5)

            if auth_failed:
                return {'status_code': 401, 'body': {'error': 'Session expired'}}

        if group_msgs > 0:
            groups_scanned.append(group_name)
        if not timed_out:
            groups_completed.append(group_name)

    # Final cleanup pass — catches anything that slipped through any code path
    for contact in contacts.values():
        # Fix 5: Remove invalid websites
        if contact.get('website') and not _is_valid_website(contact['website']):
            contact['website'] = None
        # Fix 6: Remove disclaimer text from org/title
        for field in ['organization', 'title']:
            val = contact.get(field)
            if val and _is_disclaimer_line(val):
                contact[field] = None
        # Fix 3: Move org-like title to org when org is empty (post-merge)
        if contact.get('title') and not contact.get('organization'):
            if _ORG_PATTERN.search(contact['title']) and not _TITLE_PATTERN.search(contact['title']):
                contact['organization'] = contact['title']
                contact['title'] = None

    contact_list = sorted(contacts.values(), key=lambda c: c.get('post_count', 0), reverse=True)

    result = {
        'total_contacts': len(contact_list),
        'total_messages_scanned': total_messages,
        'groups_scanned': groups_scanned,
        'groups_completed': groups_completed,
        'last_page_fetched': last_page_fetched,
        'contacts': contact_list,
    }
    if timed_out:
        result['partial'] = True
        result['next_start_group'] = timed_out_group
        result['next_start_page'] = last_page_fetched + 1
        result['message'] = f'Time budget reached. Use start_group={timed_out_group} and start_page={last_page_fetched + 1} to continue.'
    if errors:
        result['errors'] = errors

    return {'status_code': 200, 'body': result}


# === PRIVATE ===


def _merge_messages(messages, contacts, group_name):
    """Process parsed messages and merge signatures into contacts dict. Returns count of contacts with signatures."""
    sig_count = 0
    for msg in messages:
        poster_id = msg.get('sender_poster_id')
        if not poster_id:
            continue

        sig = msg.get('signature')
        if not sig:
            continue

        sig_count += 1

        sender_name = msg.get('sender_name', '')
        sender_is_email = bool(re.match(r'^[\w.+-]+@[\w.-]+\.\w+$', sender_name))

        # Fix 4: If sender_name is an email, use sig name if available
        display_name = sender_name
        if sender_is_email:
            if sig.get('name'):
                display_name = sig['name']
            # Also populate email field if we don't have one
            if not sig.get('email'):
                sig['email'] = sender_name

        if poster_id in contacts:
            existing = contacts[poster_id]
            for key in ['name', 'title', 'organization', 'phone', 'fax', 'cell', 'email', 'address', 'website']:
                if not existing.get(key) and sig.get(key):
                    existing[key] = sig[key]
            existing['post_count'] = existing.get('post_count', 0) + 1
            if group_name not in existing.get('groups_active_in', []):
                existing['groups_active_in'].append(group_name)
            if msg.get('date') and (not existing.get('latest_post_date') or msg['date'] > existing['latest_post_date']):
                existing['latest_post_date'] = msg['date']
                existing['latest_subject'] = msg.get('topic_subject', '')
            # Upgrade sender_name from email to real name if possible
            if re.match(r'^[\w.+-]+@[\w.-]+\.\w+$', existing.get('sender_name', '')):
                if sig.get('name'):
                    existing['sender_name'] = sig['name']
                elif not sender_is_email:
                    existing['sender_name'] = sender_name
        else:
            contacts[poster_id] = {
                'poster_id': poster_id,
                'sender_name': display_name,
                'name': sig.get('name'),
                'title': sig.get('title'),
                'organization': sig.get('organization'),
                'phone': sig.get('phone'),
                'fax': sig.get('fax'),
                'cell': sig.get('cell'),
                'email': sig.get('email'),
                'address': sig.get('address'),
                'website': sig.get('website'),
                'groups_active_in': [group_name],
                'post_count': 1,
                'latest_post_date': msg.get('date'),
                'latest_subject': msg.get('topic_subject', ''),
            }

    return sig_count


def _fetch_expanded_page(base_url, group_name, page, headers):
    """Fetch one page of expanded messages with retry on rate limit. Returns (html, error_string_or_None)."""
    params = "expanded=1"
    if page > 1:
        params += f"&page={page}"

    url = f"{base_url}/g/{group_name}/messages?{params}"

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.get(
                url,
                headers={**headers, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
                impersonate="chrome131",
                timeout=30,
            )
        except Exception as e:
            if attempt < MAX_RETRIES:
                time.sleep(1)
                continue
            return None, str(e)

        if response.status_code == 429:
            if attempt < MAX_RETRIES:
                time.sleep(1 + attempt)  # 1s, 2s
                continue
            return None, "HTTP 429"

        if response.status_code != 200:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "")
                if "/login" in location:
                    return None, "auth"
            # 404s are expected — deleted messages create gaps in pagination
            if response.status_code == 404:
                return None, None
            return None, f"HTTP {response.status_code}"

        page_html = response.text

        if '<input' in page_html and 'name="password"' in page_html and '/login' in page_html:
            return None, "auth"

        return page_html, None

    return None, "max retries exceeded"


def _ns_to_iso(ns_timestamp):
    """Convert nanosecond timestamp to ISO 8601 string."""
    try:
        ts_seconds = int(ns_timestamp) / 1e9
        dt = datetime.datetime.fromtimestamp(ts_seconds, tz=datetime.timezone.utc)
        return dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    except (ValueError, OSError, OverflowError):
        return None


def _html_to_text(html_content):
    """Convert HTML content to clean plain text."""
    text = html_content
    text = re.sub(r'(?<=[^>\s])\s*<div', '\n<div', text)
    text = re.sub(r'(?<=[^>\s])\s*<p[ >]', '\n<p ', text)
    text = re.sub(r'<br\s*/?\s*>', '\n', text)
    text = re.sub(r'</p>', '\n', text)
    text = re.sub(r'</div>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'<[^>]*$', '', text, flags=re.MULTILINE)
    text = html_lib.unescape(text)
    lines = text.split('\n')
    lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in lines]
    cleaned = []
    blank_count = 0
    for line in lines:
        if line == '':
            blank_count += 1
            if blank_count <= 2:
                cleaned.append(line)
        else:
            blank_count = 0
            cleaned.append(line)
    return '\n'.join(cleaned).strip()


def _extract_links(html_content):
    """Extract external URLs from href attributes."""
    links = []
    for m in re.finditer(r'href="(https?://[^"]+)"', html_content):
        url = m.group(1)
        if 'groups.io' in url:
            continue
        url = html_lib.unescape(url)
        if url not in links:
            links.append(url)
    return links


def _extract_forcebreak_content(html, start_pos):
    """Extract content from a forcebreak div by counting open/close div tags."""
    fb_marker = '<div class="forcebreak"'
    fb_pos = html.find(fb_marker, start_pos)
    if fb_pos < 0:
        return ''
    depth = 0
    i = fb_pos
    while i < len(html):
        if html[i:i+4] == '<div':
            depth += 1
        elif html[i:i+6] == '</div>':
            depth -= 1
            if depth == 0:
                inner_start = html.find('>', fb_pos) + 1
                return html[inner_start:i]
        i += 1
    return ''


def _is_likely_name(text):
    """Check if text looks like a person's name (not message body text)."""
    if not text or len(text) < 2 or len(text) > 45:
        return False
    # Strip trailing punctuation that indicates greetings ("Sophia:", "Adrienne,")
    cleaned = text.rstrip(':,;!')
    if len(cleaned) < 2:
        return False
    words = cleaned.split()
    if len(words) < 2 or len(words) > 5:
        return False  # Require at least first + last name
    # Allow suffixes like Jr., Sr., III, PE, PhD, P.E.
    name_suffixes = {'jr', 'jr.', 'sr', 'sr.', 'ii', 'iii', 'iv', 'pe', 'p.e.', 'phd', 'ph.d.', 'cpe', 'mba'}
    core_words = [w for w in words if w.lower().rstrip('.,') not in name_suffixes]
    if len(core_words) > 4:
        return False
    # Must start with uppercase
    if not words[0][0].isupper():
        return False
    # Reject sentences and phrases
    lower = cleaned.lower()
    if any(lower.startswith(p) for p in [
        'hey ', 'hello', 'hi ', 'dear ', 'to ', 'we ', 'the ', 'a ', 'an ',
        'this ', 'that ', 'it ', 'i ', 'you ', 'they ', 'our ', 'my ',
        'pdf ', 'job ', 'all', 'please', 'can ', 'do ', 'if ', 'for ',
        'see ', 'let ', 'how ', 'what ', 'when ', 'where ', 'just ',
        'no ', 'yes', 'not ', 'some', 'any',
    ]):
        return False
    # Reject lines with lowercase words (sentence fragments) — except name connectors
    connectors = {'de', 'van', 'von', 'del', 'la', 'le', 'el', 'al', 'bin', 'and', 'of'}
    for w in core_words[1:]:
        w_clean = w.rstrip('.,')
        if len(w_clean) > 2 and w_clean[0].islower() and w_clean.lower() not in connectors:
            return False
    # Reject if contains file/tech indicators
    if any(kw in lower for kw in [
        'http', 'www.', '.pdf', '.doc', 'attachment', 'confidential',
        'disclaimer', 'removed', 'message', 'portions', 'document',
        'open until', 'filled', 'position', '·', ' mb', 'inspector',
        'coordinator', 'manager', 'director', 'supervisor', 'engineer',
        'analyst', 'specialist', 'officer', 'superintendent', 'operator',
        'website', 'program', 'district', 'authority', 'utilities',
        'department', 'commission', 'bureau', 'division', 'municipal',
        'city of', 'county of', 'town of', 'village of',
    ]):
        return False
    # Reject any word longer than 20 chars
    if any(len(w) > 20 for w in words):
        return False
    # Reject pure punctuation or very short garbage
    if cleaned.strip('|><-.,!? ') == '':
        return False
    return True


def _is_disclaimer_line(line):
    """Check if a line is boilerplate disclaimer/footer text."""
    lower = line.lower()
    return any(kw in lower for kw in [
        'confidential', 'disclaimer', 'privileged', 'intended recipient',
        'unauthorized', 'non-text portions', 'this message', 'this email',
        'e-mail disclaimer', 'legal notice', 'do not print',
        'if you are not the', 'if you have received this',
        'automatically generated', 'sent from my iphone', 'sent from my ipad',
    ])


def _is_valid_website(url):
    """Check if URL is likely a personal/org website, not a document link."""
    if not url:
        return False
    # Strip trailing pipe, whitespace, common punctuation artifacts
    url = url.rstrip('|> \t')
    lower = url.lower()
    if 'groups.io' in lower or 'google.com/maps' in lower:
        return False
    # Reject document URLs — check both path ending and anywhere in URL
    doc_exts = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.zip', '.png', '.jpg']
    for ext in doc_exts:
        if lower.endswith(ext) or (ext + '?') in lower or (ext + '#') in lower:
            return False
    # Reject URLs with document path segments (government doc archives)
    if re.search(r'/(?:documents|files|pubs|pub)/.*\.(?:pdf|doc|docx|xls|xlsx|ppt|pptx)', lower):
        return False
    # Reject known non-website URLs
    if any(d in lower for d in [
        '/pubs/', '/pub/', '/npdes/', 'regulations.gov', 'change.org',
        'petition', 'gofundme', 'governmentjobs', 'mailto:',
        'safelinks.protection', 'urldefense.proofpoint', 'aka.ms/',
        'outlook-sdf.office.com', 'bookwithme', 'linkedin.com/',
        'mypronouns.org', 'hihello.me', 'nam11.safelinks',
        'facebook.com/', 'surveymonkey.com', 'twitter.com/',
        '/sites/default/files/', '/sites/production/files/',
    ]):
        return False
    return True


_ORG_PATTERN = re.compile(
    r'\b(City|County|Town|Village|Borough|District|Department|Dept|Division|Authority|Bureau|Commission|'
    r'Agency|Board|Inc|LLC|Corp|Company|Utilities|University|College|Association|Foundation|Institute|'
    r'Municipal|Metropolitan|Regional|Public Works|Water|Sewer|Sanitation|Sanitary)\b', re.IGNORECASE)

_TITLE_PATTERN = re.compile(
    r'\b(Manager|Director|Supervisor|Coordinator|Officer|Engineer|Analyst|Specialist|Inspector|'
    r'Administrator|Superintendent|Chief|Lead|Senior|Principal|Technician|Operator|Planner|Scientist|'
    r'Writer|Chemist|Biologist|Consultant|Advisor|Representative|Foreman|Compliance|Pretreatment|'
    r'President|Vice President|VP|CEO|CFO|COO|Secretary|Treasurer)\b', re.IGNORECASE)


def _classify_title_org(title_val, org_val):
    """Swap title and org if they appear to be in the wrong fields."""
    if not title_val or not org_val:
        return title_val, org_val
    title_has_org = bool(_ORG_PATTERN.search(title_val))
    org_has_title = bool(_TITLE_PATTERN.search(org_val))
    if title_has_org and org_has_title:
        return org_val, title_val  # Swap
    if title_has_org and not _TITLE_PATTERN.search(title_val):
        return org_val, title_val  # Title looks like org name
    if org_has_title and not _ORG_PATTERN.search(org_val):
        return org_val, title_val  # Org looks like a title
    return title_val, org_val


def _parse_signature(body_text, body_html):
    """Best-effort extraction of signature fields from message body."""
    sig = {
        'name': None, 'title': None, 'organization': None,
        'phone': None, 'fax': None, 'cell': None,
        'email': None, 'address': None, 'website': None,
    }

    sig_html = ''
    sig_block_found = False  # True when we found a clear signature boundary

    # Try gmail_signature block (highest confidence)
    sig_div_pos = body_html.find('class="gmail_signature"')
    if sig_div_pos >= 0:
        div_start = body_html.rfind('<div', 0, sig_div_pos)
        if div_start >= 0:
            depth = 0
            i = div_start
            while i < len(body_html):
                if body_html[i:i+4] == '<div':
                    depth += 1
                elif body_html[i:i+6] == '</div>':
                    depth -= 1
                    if depth == 0:
                        inner_start = body_html.find('>', div_start) + 1
                        sig_html = body_html[inner_start:i]
                        sig_block_found = True
                        break
                i += 1

    # Try separator patterns (medium confidence)
    if not sig_html:
        for sep_pattern in [
            r'(?:Regards|Thank you|Thanks|Best regards|Sincerely|Best)\s*,?\s*</(?:p|div|span)>',
            r'(?:Regards|Thank you|Thanks|Best regards|Sincerely|Best)\s*,?\s*\n',
        ]:
            sep_match = re.search(sep_pattern, body_html, re.IGNORECASE)
            if sep_match:
                sig_html = body_html[sep_match.end():]
                sig_block_found = True
                break

    # Fallback: last portion of body (low confidence — only extract labeled fields)
    if not sig_html:
        sig_html = body_html[-1500:] if len(body_html) > 1500 else body_html

    sig_text = _html_to_text(sig_html) if sig_html else body_text[-500:] if len(body_text) > 500 else body_text

    # Extract labeled contact fields (reliable regardless of confidence)
    phone_patterns = [
        (r'(?:Phone|Ph?|Tel|Office|Direct|Work)\s*[.:]\s*([\d\s().-]{10,20})', 'phone'),
        (r'(?:Fax|F)\s*[.:]\s*([\d\s().-]{10,20})', 'fax'),
        (r'(?:Cell|Mobile|M)\s*[.:]\s*([\d\s().-]{10,20})', 'cell'),
    ]
    for pattern, field in phone_patterns:
        m = re.search(pattern, sig_text, re.IGNORECASE)
        if m:
            sig[field] = m.group(1).strip()

    # Extract email
    email_match = re.search(r'[\w.+-]+@[\w.-]+\.[\w]+', sig_text)
    if not email_match:
        email_match = re.search(r'[\w.+-]+@\.\.\.', sig_text)
    if email_match:
        sig['email'] = email_match.group(0)
    if not sig['email']:
        mailto_match = re.search(r'mailto:([\w.+-]+@[^\s"&]+)', sig_html)
        if mailto_match:
            sig['email'] = html_lib.unescape(mailto_match.group(1))

    # Extract website — only from identified sig blocks, with validation
    if sig_block_found:
        website_match = re.search(r'(?:www\.\S+|https?://\S+)', sig_text)
        if website_match:
            url = website_match.group(0).rstrip('.,;)|> ')
            if _is_valid_website(url):
                sig['website'] = url
        if not sig['website']:
            for m in re.finditer(r'href="(https?://(?:www\.)?[^"]+)"', sig_html):
                url = html_lib.unescape(m.group(1))
                if _is_valid_website(url):
                    sig['website'] = url
                    break

    # Extract address
    addr_match = re.search(r'(\d+[^,\n]*,\s*[A-Z][a-z]+[^,\n]*,?\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?)', sig_text)
    if addr_match:
        sig['address'] = addr_match.group(1).strip()

    has_contact = any(sig[k] for k in ['phone', 'fax', 'cell', 'email', 'address'])

    # Extract name/title/org only when:
    # - We found a clear sig block boundary, OR
    # - We found labeled contact info (proving a signature exists even without a boundary)
    if sig_block_found or has_contact:
        sig_lines = [l.strip() for l in sig_text.split('\n') if l.strip()]
        name_candidate_lines = []
        for line in sig_lines[:8]:
            if re.search(r'(?:Phone|Tel|Fax|Cell|Office|Direct|Mobile|P:|F:|M:)\s*[.:]', line, re.IGNORECASE):
                continue
            if re.search(r'@|www\.|http|\.com|\.org|\.gov|\.net', line, re.IGNORECASE):
                continue
            if re.search(r'\d{3}[.\s-]\d{3}[.\s-]\d{4}|\(\d{3}\)', line):
                continue
            if re.search(r'\d{5}', line):
                continue
            if len(line) > 80:
                continue
            if line.startswith(('Regards', 'Thank', 'Best', 'Sincerely', '--', 'Sent from')):
                continue
            if re.match(r'^\d+\s', line):
                continue
            if re.search(r'[={}"]|class=|style=', line):
                continue
            if _is_disclaimer_line(line):
                continue
            name_candidate_lines.append(line)

        if name_candidate_lines:
            first_line = name_candidate_lines[0]
            if '|' in first_line:
                parts = [p.strip() for p in first_line.split('|', 1)]
                if _is_likely_name(parts[0]):
                    sig['name'] = parts[0].rstrip(':,;!')
                if len(parts) > 1 and parts[1] and len(parts[1]) > 2:
                    sig['title'] = parts[1]
                    if len(name_candidate_lines) > 1:
                        sig['organization'] = name_candidate_lines[1]
                else:
                    if len(name_candidate_lines) > 1:
                        sig['title'] = name_candidate_lines[1]
                    if len(name_candidate_lines) > 2:
                        sig['organization'] = name_candidate_lines[2]
            else:
                if _is_likely_name(first_line):
                    sig['name'] = first_line.rstrip(':,;!')
                    if len(name_candidate_lines) > 1:
                        sig['title'] = name_candidate_lines[1]
                    if len(name_candidate_lines) > 2:
                        sig['organization'] = name_candidate_lines[2]

        # Clean up garbage values
        for field in ['name', 'title', 'organization']:
            val = sig[field]
            if val and (len(val) <= 2 or val.strip('|><-.,!? ') == ''):
                sig[field] = None
            if val and _is_disclaimer_line(val):
                sig[field] = None
        # Strip pronoun tags from name: "Julie Faas (she/her)" -> "Julie Faas"
        if sig['name']:
            sig['name'] = re.sub(r'\s*\([^)]*(?:she|her|him|his|they|them|he)\s*/[^)]*\)', '', sig['name']).strip()

        # Fix swapped title/org
        if sig['title'] and sig['organization']:
            sig['title'], sig['organization'] = _classify_title_org(sig['title'], sig['organization'])
        # Move title to org when title is clearly an org name and org is empty
        elif sig['title'] and not sig['organization']:
            if _ORG_PATTERN.search(sig['title']) and not _TITLE_PATTERN.search(sig['title']):
                sig['organization'] = sig['title']
                sig['title'] = None

    # Return None if nothing useful found
    if not has_contact and not sig['name']:
        return None

    return sig


def _parse_expanded_messages(page_html):
    """Parse messages from the expanded messages view."""
    messages = []

    anchors = [(m.start(), m.group(1)) for m in re.finditer(r'<a name="msg(\d+)"', page_html)]
    bodies = [(m.start(), m.group(1)) for m in re.finditer(r'id="msgbody(\d+)"', page_html)]

    for body_pos, msg_id in bodies:
        message = {}
        message['message_id'] = msg_id

        message_number = None
        for anchor_pos, anchor_num in anchors:
            if anchor_pos < body_pos:
                message_number = int(anchor_num)
            else:
                break
        message['message_number'] = message_number

        header_start = 0
        for anchor_pos, anchor_num in anchors:
            if int(anchor_num) == message_number:
                header_start = anchor_pos
                break
        header_html = page_html[header_start:body_pos]

        # Topic subject
        subject_match = re.search(r'data-subject="([^"]+)"', header_html)
        if subject_match:
            message['topic_subject'] = html_lib.unescape(subject_match.group(1))
        else:
            subj_span = re.search(r'<span class="subject">\s*(.*?)\s*</span>', header_html, re.DOTALL)
            if subj_span:
                subj = re.sub(r'<[^>]+>', '', subj_span.group(1)).strip()
                subj = re.sub(r'^Re:\s*', '', subj).strip()
                message['topic_subject'] = subj
            else:
                message['topic_subject'] = ''

        # Sender
        sender_match = re.search(r'<span class="user-chip-name">\s*(.*?)\s*</span>', header_html, re.DOTALL)
        message['sender_name'] = sender_match.group(1).strip() if sender_match else ''

        poster_match = re.search(r'posterid:(\d+)', header_html)
        message['sender_poster_id'] = poster_match.group(1) if poster_match else None

        # Date
        time_match = re.search(rf'timedispmsg{msg_id}.*?DisplayShortTime\((\d+),', header_html, re.DOTALL)
        message['date'] = _ns_to_iso(time_match.group(1)) if time_match else None

        # Body
        body_html = _extract_forcebreak_content(page_html, body_pos)
        body_text = _html_to_text(body_html) if body_html else ''

        # Signature
        message['signature'] = _parse_signature(body_text, body_html) if body_html else None

        messages.append(message)

    return messages
