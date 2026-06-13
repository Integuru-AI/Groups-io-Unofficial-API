from curl_cffi import requests
import re
import datetime
import html as html_lib

BASE_URL = "https://groups.io"
APP_URL = "https://groups.io"


def run(headers, user_input):
    """Get all messages from a groups.io topic with sender details, content, signatures, and metadata."""
    base_url = BASE_URL

    group_name = user_input.get("group_name")
    if not group_name:
        return {'status_code': 400, 'body': {'error': 'group_name is required'}}

    thread_id = user_input.get("thread_id")
    topic_slug = user_input.get("topic_slug", "")
    sort_order = user_input.get("sort_order", "asc")  # "asc" (oldest first) or "desc" (newest first)

    if not thread_id:
        return {'status_code': 400, 'body': {'error': 'thread_id is required'}}

    try:
        page_html = _fetch_topic_page(base_url, group_name, thread_id, topic_slug, headers, sort_order)
    except PermissionError as e:
        return {'status_code': 401, 'body': {'error': str(e)}}
    except RuntimeError as e:
        return {'status_code': 500, 'body': {'error': str(e)}}

    # Extract topic title from page
    title_match = re.search(r'<title>([^<]+)</title>', page_html)
    topic_title = ''
    if title_match:
        topic_title = title_match.group(1).strip()
        if '|' in topic_title:
            topic_title = topic_title.split('|', 1)[-1].strip()

    # Parse messages
    messages = _parse_messages(page_html, group_name)

    return {
        'status_code': 200,
        'body': {
            'topic_title': topic_title,
            'group_name': group_name,
            'thread_id': thread_id,
            'message_count': len(messages),
            'messages': messages
        }
    }


# === PRIVATE ===


def _fetch_topic_page(base_url, group_name, thread_id, topic_slug, headers, sort_order="asc"):
    """Fetch the topic HTML page from groups.io."""
    if topic_slug:
        url = f"{base_url}/g/{group_name}/topic/{topic_slug}/{thread_id}"
    else:
        url = f"{base_url}/g/{group_name}/topic/{thread_id}"

    if sort_order == "desc":
        url += "?dir=desc"

    response = requests.get(
        url,
        headers={**headers, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        impersonate="chrome131",
        timeout=30,
    )

    if response.status_code != 200:
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location", "")
            if "/login" in location:
                raise PermissionError("Session expired")
        raise RuntimeError(f"Request failed with status {response.status_code}")

    page_html = response.text

    # Check for login page (session expired returns 200 with login form)
    if '<input' in page_html and 'name="password"' in page_html and '/login' in page_html:
        raise PermissionError("Session expired")

    return page_html


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
    # Insert newline before opening block-level tags to handle "text<div>more text"
    text = re.sub(r'(?<=[^>\s])\s*<div', '\n<div', text)
    text = re.sub(r'(?<=[^>\s])\s*<p[ >]', '\n<p ', text)
    # Replace <br>, <br/>, </p>, </div> with newlines
    text = re.sub(r'<br\s*/?\s*>', '\n', text)
    text = re.sub(r'</p>', '\n', text)
    text = re.sub(r'</div>', '\n', text)
    # Remove all HTML tags (including malformed/partial)
    text = re.sub(r'<[^>]+>', '', text)
    # Clean up any remaining tag-like artifacts
    text = re.sub(r'<[^>]*$', '', text, flags=re.MULTILINE)  # Unclosed tags at end of line
    # Decode HTML entities
    text = html_lib.unescape(text)
    # Clean up whitespace: collapse multiple spaces on same line, preserve newlines
    lines = text.split('\n')
    lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in lines]
    # Remove excessive blank lines (keep max 2 consecutive)
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
    """Extract all URLs from href attributes in HTML content."""
    links = []
    for m in re.finditer(r'href="(https?://[^"]+)"', html_content):
        url = m.group(1)
        # Skip groups.io internal links and Google Maps/tracking links
        if 'groups.io' in url:
            continue
        url = html_lib.unescape(url)
        if url not in links:
            links.append(url)
    return links


def _extract_attachments(html_content, group_name):
    """Extract attachment info from the message HTML."""
    attachments = []
    # Attachments: <img ... src="/g/{group}/attachment/{id}/{index}..."
    for m in re.finditer(
        r'data-lightbox-title="([^"]*)"[^>]*data-lightbox-download="([^"]*)"',
        html_content
    ):
        name = m.group(1)
        url = m.group(2)
        if not url.startswith('http'):
            url = f"https://groups.io{url}"
        attachments.append({'name': name, 'url': url})

    # Also check for download links: data-lightbox-download="..."
    if not attachments:
        for m in re.finditer(
            r'src="(/g/[^"]+/attachment/[^"]+)"[^>]*data-lightbox-src',
            html_content
        ):
            url = f"https://groups.io{m.group(1)}"
            attachments.append({'name': '', 'url': url})

    return attachments


def _parse_signature(body_text, body_html):
    """Best-effort extraction of signature fields from message body."""
    sig = {
        'name': None,
        'title': None,
        'organization': None,
        'phone': None,
        'fax': None,
        'cell': None,
        'email': None,
        'address': None,
        'website': None,
    }

    # Try to find the signature block in HTML first
    # Common patterns: gmail_signature, after "-- " separator, or at end of message
    sig_html = ''

    # Use div-counting to extract full gmail_signature content (handles nested divs)
    sig_div_pos = body_html.find('class="gmail_signature"')
    if sig_div_pos >= 0:
        # Find the opening <div that contains this class
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
                        break
                i += 1

    # If no explicit signature block, try to find a signature separator
    # Common patterns: "Regards,", "Thank you,", "Best,", "Sincerely," followed by contact info
    if not sig_html:
        # Look for common sig separators and take everything after them
        for sep_pattern in [
            r'(?:Regards|Thank you|Thanks|Best regards|Sincerely|Best)\s*,?\s*</(?:p|div|span)>',
            r'(?:Regards|Thank you|Thanks|Best regards|Sincerely|Best)\s*,?\s*\n',
        ]:
            sep_match = re.search(sep_pattern, body_html, re.IGNORECASE)
            if sep_match:
                sig_html = body_html[sep_match.end():]
                break

    # If still no sig, use the last portion of the body
    if not sig_html:
        sig_html = body_html[-1500:] if len(body_html) > 1500 else body_html

    sig_text = _html_to_text(sig_html) if sig_html else body_text[-500:] if len(body_text) > 500 else body_text

    # Extract phone numbers
    phone_patterns = [
        (r'(?:Phone|Ph?|Tel|Office|Direct|Work)\s*[.:]\s*([\d\s().-]{10,20})', 'phone'),
        (r'(?:Fax|F)\s*[.:]\s*([\d\s().-]{10,20})', 'fax'),
        (r'(?:Cell|Mobile|M)\s*[.:]\s*([\d\s().-]{10,20})', 'cell'),
    ]
    for pattern, field in phone_patterns:
        m = re.search(pattern, sig_text, re.IGNORECASE)
        if m:
            sig[field] = m.group(1).strip()

    # Extract email (may be redacted as user@...)
    email_match = re.search(r'[\w.+-]+@[\w.-]+\.[\w]+', sig_text)
    if not email_match:
        # Check for redacted format
        email_match = re.search(r'[\w.+-]+@\.\.\.', sig_text)
    if email_match:
        sig['email'] = email_match.group(0)

    # Also check href="mailto:..." in HTML
    if not sig['email']:
        mailto_match = re.search(r'mailto:([\w.+-]+@[^\s"&]+)', sig_html)
        if mailto_match:
            sig['email'] = html_lib.unescape(mailto_match.group(1))

    # Extract website
    website_match = re.search(r'(?:www\.\S+|https?://\S+)', sig_text)
    if website_match:
        url = website_match.group(0).rstrip('.,;)')
        if 'groups.io' not in url and 'google.com/maps' not in url:
            sig['website'] = url

    # Also check for website links in HTML
    if not sig['website']:
        for m in re.finditer(r'href="(https?://(?:www\.)?[^"]+)"', sig_html):
            url = m.group(1)
            if ('groups.io' not in url and 'google.com/maps' not in url
                    and 'mailto:' not in url and 'governmentjobs' not in url):
                sig['website'] = html_lib.unescape(url)
                break

    # Extract address (look for state abbreviation + zip pattern)
    addr_match = re.search(r'(\d+[^,\n]*,\s*[A-Z][a-z]+[^,\n]*,?\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?)', sig_text)
    if addr_match:
        sig['address'] = addr_match.group(1).strip()

    # Check if any phone/contact fields were found — if so, likely a real signature
    has_contact = any(sig[k] for k in ['phone', 'fax', 'cell', 'email', 'website', 'address'])

    if has_contact:
        # Try to extract name and title from the signature area
        # Usually the first 1-3 lines of a signature are name, title, org
        sig_lines = [l.strip() for l in sig_text.split('\n') if l.strip()]

        # Filter out lines that are clearly contact info or content
        name_candidate_lines = []
        for line in sig_lines[:8]:
            # Skip lines that are phone numbers, emails, addresses, URLs, or content
            if re.search(r'(?:Phone|Tel|Fax|Cell|Office|Direct|Mobile|P:|F:|M:)\s*[.:]', line, re.IGNORECASE):
                continue
            if re.search(r'@|www\.|http|\.com|\.org|\.gov|\.net', line, re.IGNORECASE):
                continue
            if re.search(r'\d{3}[.\s-]\d{3}[.\s-]\d{4}|\(\d{3}\)', line):
                continue
            if re.search(r'\d{5}', line):  # Zip code = address line
                continue
            if len(line) > 80:  # Too long for a name/title
                continue
            if line.startswith(('Regards', 'Thank', 'Best', 'Sincerely', '--', 'Sent from')):
                continue
            if re.match(r'^\d+\s', line):  # Starts with number = likely address
                continue
            if re.search(r'[={}"]|class=|style=', line):  # HTML artifact
                continue
            name_candidate_lines.append(line)

        # First candidate is usually name, second is title, third might be org
        if name_candidate_lines:
            first_line = name_candidate_lines[0]
            # Handle "Name | Title" or "Name, Title" patterns
            if '|' in first_line:
                parts = [p.strip() for p in first_line.split('|', 1)]
                sig['name'] = parts[0]
                if len(parts) > 1 and parts[1]:
                    sig['title'] = parts[1]
                    # Shift: next candidate becomes org
                    if len(name_candidate_lines) > 1:
                        sig['organization'] = name_candidate_lines[1]
                else:
                    if len(name_candidate_lines) > 1:
                        sig['title'] = name_candidate_lines[1]
                    if len(name_candidate_lines) > 2:
                        sig['organization'] = name_candidate_lines[2]
            else:
                sig['name'] = first_line
                if len(name_candidate_lines) > 1:
                    sig['title'] = name_candidate_lines[1]
                if len(name_candidate_lines) > 2:
                    sig['organization'] = name_candidate_lines[2]

    # Return None for the whole signature if nothing was found
    if not has_contact and not sig['name']:
        return None

    return sig


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
                # Return inner content (skip the outer forcebreak div tag)
                inner_start = html.find('>', fb_pos) + 1
                return html[inner_start:i]
        i += 1
    return ''


def _parse_messages(page_html, group_name):
    """Parse all messages from a topic page."""
    messages = []

    # Build ordered lists of msg anchors and msgbody positions
    anchors = [(m.start(), m.group(1)) for m in re.finditer(r'<a name="msg(\d+)"', page_html)]
    bodies = [(m.start(), m.group(1)) for m in re.finditer(r'id="msgbody(\d+)"', page_html)]

    for body_pos, msg_id in bodies:
        message = {}
        message['message_id'] = msg_id

        # Find the nearest preceding anchor for this message body
        message_number = None
        for anchor_pos, anchor_num in anchors:
            if anchor_pos < body_pos:
                message_number = int(anchor_num)
            else:
                break
        message['message_number'] = message_number

        # Get the header section (between the anchor and the msgbody div)
        header_start = 0
        for anchor_pos, anchor_num in anchors:
            if int(anchor_num) == message_number:
                header_start = anchor_pos
                break
        header_html = page_html[header_start:body_pos]

        # Sender name from user-chip in the header
        sender_match = re.search(
            r'<span class="user-chip-name">\s*(.*?)\s*</span>',
            header_html, re.DOTALL
        )
        message['sender_name'] = sender_match.group(1).strip() if sender_match else ''

        # Sender poster ID (from the search link in header)
        poster_match = re.search(r'posterid:(\d+)', header_html)
        message['sender_poster_id'] = poster_match.group(1) if poster_match else None

        # Sender avatar URL
        avatar_match = re.search(
            r'<img src="([^"]*profilephoto[^"]*)"',
            header_html
        )
        message['sender_avatar_url'] = html_lib.unescape(avatar_match.group(1)) if avatar_match else None

        # Date/time from DisplayShortTime call in header
        time_match = re.search(
            rf'timedispmsg{msg_id}.*?DisplayShortTime\((\d+),',
            header_html, re.DOTALL
        )
        message['date'] = _ns_to_iso(time_match.group(1)) if time_match else None

        # Message body HTML — use div-counting approach for nested content
        body_html = _extract_forcebreak_content(page_html, body_pos)

        # Convert to plain text
        message['body_text'] = _html_to_text(body_html) if body_html else ''

        # Extract signature
        message['signature'] = _parse_signature(message['body_text'], body_html)

        # Extract links from body
        message['links'] = _extract_links(body_html)

        # Extract attachments
        message['attachments'] = _extract_attachments(body_html, group_name)

        # Has likes (check if displayLikeStats is called with a non-zero count)
        likes_match = re.search(rf'displayLikeStats\("[^"]*",\s*(\d+),\s*{msg_id}', page_html)
        message['has_likes'] = bool(likes_match and int(likes_match.group(1)) > 0)

        messages.append(message)

    return messages
