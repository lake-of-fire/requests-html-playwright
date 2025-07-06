import sys
import asyncio
import traceback
from urllib.parse import urlparse, urlunparse, urljoin
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures._base import TimeoutError
from functools import partial
from typing import Set, Union, List, MutableMapping, Optional

import lxml_html_clean
import requests
import http.cookiejar

from playwright.async_api import Playwright, Browser, async_playwright
from pyquery import PyQuery

from fake_useragent import UserAgent
import lxml
from lxml import etree
from lxml.html import HtmlElement
from lxml.html import tostring as lxml_html_tostring
from lxml.html.soupparser import fromstring as soup_parse
from parse import search as parse_search
from parse import findall, Result
from w3lib.encoding import html_to_unicode

DEFAULT_ENCODING = 'utf-8'
DEFAULT_URL = 'https://example.org/'
DEFAULT_USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_12_6) AppleWebKit/603.3.8 (KHTML, like Gecko) Version/10.1.2 Safari/603.3.8'
DEFAULT_NEXT_SYMBOL = ['next', 'more', 'older']

cleaner = lxml_html_clean.Cleaner()
cleaner.javascript = True
cleaner.style = True

useragent = None

# Typing.
_Find = Union[List['Element'], 'Element']
_XPath = Union[List[str], List['Element'], str, 'Element']
_Result = Union[List['Result'], 'Result']
_HTML = Union[str, bytes]
_BaseHTML = str
_UserAgent = str
_DefaultEncoding = str
_URL = str
_RawHTML = bytes
_Encoding = str
_LXML = HtmlElement
_Text = str
_Search = Result
_Containing = Union[str, List[str]]
_Links = Set[str]
_Attrs = MutableMapping
_Next = Union['HTML', List[str]]
_NextSymbol = List[str]

# Sanity checking.
try:
    assert sys.version_info.major == 3
    assert sys.version_info.minor > 5
except AssertionError:
    raise RuntimeError('Requests-HTML requires Python 3.6+!')


class MaxRetries(Exception):

    def __init__(self, message):
        self.message = message


class BaseParser:
    """A basic HTML/Element Parser, for Humans.

    :param element: The element from which to base the parsing upon.
    :param default_encoding: Which encoding to default to.
    :param html: HTML from which to base the parsing upon (optional).
    :param url: The URL from which the HTML originated, used for ``absolute_links``.

    """

    def __init__(self, *, element, default_encoding: _DefaultEncoding = None, html: _HTML = None, url: _URL) -> None:
        self.element = element
        self.url = url
        self.skip_anchors = True
        self.default_encoding = default_encoding
        self._encoding = None
        self._html = html.encode(DEFAULT_ENCODING) if isinstance(html, str) else html
        self._lxml = None
        self._pq = None

    @property
    def raw_html(self) -> _RawHTML:
        """Bytes representation of the HTML content.
        (`learn more <http://www.diveintopython3.net/strings.html>`_).
        """
        if self._html:
            return self._html
        else:
            return etree.tostring(self.element, encoding='unicode').strip().encode(self.encoding)

    @property
    def html(self) -> _BaseHTML:
        """Unicode representation of the HTML content
        (`learn more <http://www.diveintopython3.net/strings.html>`_).
        """
        if self._html:
            return self.raw_html.decode(self.encoding, errors='replace')
        else:
            return etree.tostring(self.element, encoding='unicode').strip()

    @html.setter
    def html(self, html: str) -> None:
        self._html = html.encode(self.encoding)

    @raw_html.setter
    def raw_html(self, html: bytes) -> None:
        """Property setter for self.html."""
        self._html = html

    @property
    def encoding(self) -> _Encoding:
        """The encoding string to be used, extracted from the HTML and
        :class:`HTMLResponse <HTMLResponse>` headers.
        """
        if self._encoding:
            return self._encoding

        # Scan meta tags for charset.
        if self._html:
            self._encoding = html_to_unicode(self.default_encoding, self._html)[0]
            # Fall back to requests' detected encoding if decode fails.
            try:
                self.raw_html.decode(self.encoding, errors='replace')
            except UnicodeDecodeError:
                self._encoding = self.default_encoding

        return self._encoding if self._encoding else self.default_encoding

    @encoding.setter
    def encoding(self, enc: str) -> None:
        """Property setter for self.encoding."""
        self._encoding = enc

    @property
    def pq(self) -> PyQuery:
        """`PyQuery <https://pythonhosted.org/pyquery/>`_ representation
        of the :class:`Element <Element>` or :class:`HTML <HTML>`.
        """
        if self._pq is None:
            self._pq = PyQuery(self.lxml)

        return self._pq

    @property
    def lxml(self) -> HtmlElement:
        """`lxml <http://lxml.de>`_ representation of the
        :class:`Element <Element>` or :class:`HTML <HTML>`.
        """
        if self._lxml is None:
            try:
                self._lxml = soup_parse(self.html, features='html.parser')
            except ValueError:
                self._lxml = lxml.html.fromstring(self.raw_html)

        return self._lxml

    @property
    def text(self) -> _Text:
        """The text content of the
        :class:`Element <Element>` or :class:`HTML <HTML>`.
        """
        return self.pq.text()

    @property
    def full_text(self) -> _Text:
        """The full text content (including links) of the
        :class:`Element <Element>` or :class:`HTML <HTML>`.
        """
        return self.lxml.text_content()

    def find(self, selector: str = "*", *, containing: _Containing = None, clean: bool = False, first: bool = False,
             _encoding: str = None) -> _Find:
        """Given a CSS Selector, returns a list of
        :class:`Element <Element>` objects or a single one.

        :param selector: CSS Selector to use.
        :param clean: Whether or not to sanitize the found HTML of ``<script>`` and ``<style>`` tags.
        :param containing: If specified, only return elements that contain the provided text.
        :param first: Whether or not to return just the first result.
        :param _encoding: The encoding format.

        Example CSS Selectors:

        - ``a``
        - ``a.someClass``
        - ``a#someID``
        - ``a[target=_blank]``

        See W3School's `CSS Selectors Reference
        <https://www.w3schools.com/cssref/css_selectors.asp>`_
        for more details.

        If ``first`` is ``True``, only returns the first
        :class:`Element <Element>` found.
        """

        # Convert a single containing into a list.
        if isinstance(containing, str):
            containing = [containing]

        encoding = _encoding or self.encoding
        elements = [
            Element(element=found, url=self.url, default_encoding=encoding)
            for found in self.pq(selector)
        ]

        if containing:
            elements_copy = elements.copy()
            elements = []

            for element in elements_copy:
                if any([c.lower() in element.full_text.lower() for c in containing]):
                    elements.append(element)

            elements.reverse()

        # Sanitize the found HTML.
        if clean:
            elements_copy = elements.copy()
            elements = []

            for element in elements_copy:
                element.raw_html = lxml_html_tostring(cleaner.clean_html(element.lxml))
                elements.append(element)

        return _get_first_or_list(elements, first)

    def xpath(self, selector: str, *, clean: bool = False, first: bool = False, _encoding: str = None) -> _XPath:
        """Given an XPath selector, returns a list of
        :class:`Element <Element>` objects or a single one.

        :param selector: XPath Selector to use.
        :param clean: Whether or not to sanitize the found HTML of ``<script>`` and ``<style>`` tags.
        :param first: Whether or not to return just the first result.
        :param _encoding: The encoding format.

        If a sub-selector is specified (e.g. ``//a/@href``), a simple
        list of results is returned.

        See W3School's `XPath Examples
        <https://www.w3schools.com/xml/xpath_examples.asp>`_
        for more details.

        If ``first`` is ``True``, only returns the first
        :class:`Element <Element>` found.
        """
        selected = self.lxml.xpath(selector)

        elements = [
            Element(element=selection, url=self.url, default_encoding=_encoding or self.encoding)
            if not isinstance(selection, etree._ElementUnicodeResult) else str(selection)
            for selection in selected
        ]

        # Sanitize the found HTML.
        if clean:
            elements_copy = elements.copy()
            elements = []

            for element in elements_copy:
                element.raw_html = lxml_html_tostring(cleaner.clean_html(element.lxml))
                elements.append(element)

        return _get_first_or_list(elements, first)

    def search(self, template: str) -> Result:
        """Search the :class:`Element <Element>` for the given Parse template.

        :param template: The Parse template to use.
        """

        return parse_search(template, self.html)

    def search_all(self, template: str) -> _Result:
        """Search the :class:`Element <Element>` (multiple times) for the given parse
        template.

        :param template: The Parse template to use.
        """
        return [r for r in findall(template, self.html)]

    @property
    def links(self) -> _Links:
        """All found links on page, in as–is form."""

        def gen():
            for link in self.find('a'):

                try:
                    href = link.attrs['href'].strip()
                    if href and not (href.startswith('#') and self.skip_anchors) and not href.startswith(
                            ('javascript:', 'mailto:')):
                        yield href
                except KeyError:
                    pass

        return set(gen())

    def _make_absolute(self, link):
        """Makes a given link absolute."""

        # Parse the link with stdlib.
        parsed = urlparse(link)._asdict()

        # If link is relative, then join it with base_url.
        if not parsed['netloc']:
            return urljoin(self.base_url, link)

        # Link is absolute; if it lacks a scheme, add one from base_url.
        if not parsed['scheme']:
            parsed['scheme'] = urlparse(self.base_url).scheme

            # Reconstruct the URL to incorporate the new scheme.
            parsed = (v for v in parsed.values())
            return urlunparse(parsed)

        # Link is absolute and complete with scheme; nothing to be done here.
        return link

    @property
    def absolute_links(self) -> _Links:
        """All found links on page, in absolute form
        (`learn more <https://www.navegabem.com/absolute-or-relative-links.html>`_).
        """

        def gen():
            for link in self.links:
                yield self._make_absolute(link)

        return set(gen())

    @property
    def base_url(self) -> _URL:
        """The base URL for the page. Supports the ``<base>`` tag
        (`learn more <https://www.w3schools.com/tags/tag_base.asp>`_)."""

        # Support for <base> tag.
        base = self.find('base', first=True)
        if base:
            result = base.attrs.get('href', '').strip()
            if result:
                return result

        # Parse the url to separate out the path
        parsed = urlparse(self.url)._asdict()

        # Remove any part of the path after the last '/'
        parsed['path'] = '/'.join(parsed['path'].split('/')[:-1]) + '/'

        # Reconstruct the url with the modified path
        parsed = (v for v in parsed.values())
        url = urlunparse(parsed)

        return url


class Element(BaseParser):
    """An element of HTML.

    :param element: The element from which to base the parsing upon.
    :param url: The URL from which the HTML originated, used for ``absolute_links``.
    :param default_encoding: Which encoding to default to.
    """

    __slots__ = [
        'element', 'url', 'skip_anchors', 'default_encoding', '_encoding',
        '_html', '_lxml', '_pq', '_attrs', 'session'
    ]

    def __init__(self, *, element, url: _URL, default_encoding: _DefaultEncoding = None) -> None:
        super(Element, self).__init__(element=element, url=url, default_encoding=default_encoding)
        self.element = element
        self.tag = element.tag
        self.lineno = element.sourceline
        self._attrs = None

    def __repr__(self) -> str:
        attrs = ['{}={}'.format(attr, repr(self.attrs[attr])) for attr in self.attrs]
        return "<Element {} {}>".format(repr(self.element.tag), ' '.join(attrs))

    @property
    def attrs(self) -> _Attrs:
        """Returns a dictionary of the attributes of the :class:`Element <Element>`
        (`learn more <https://www.w3schools.com/tags/ref_attributes.asp>`_).
        """
        if self._attrs is None:
            self._attrs = {k: v for k, v in self.element.items()}

            # Split class and rel up, as there are usually many of them:
            for attr in ['class', 'rel']:
                if attr in self._attrs:
                    self._attrs[attr] = tuple(self._attrs[attr].split())

        return self._attrs


class HTML(BaseParser):
    """An HTML document, ready for parsing.

    :param url: The URL from which the HTML originated, used for ``absolute_links``.
    :param html: HTML from which to base the parsing upon (optional).
    :param default_encoding: Which encoding to default to.
    """

    def __init__(self, *, session: Union['HTMLSession', 'AsyncHTMLSession'] = None, url: str = DEFAULT_URL, html: _HTML,
                 default_encoding: str = DEFAULT_ENCODING, async_: bool = False) -> None:

        # Convert incoming unicode HTML into bytes.
        if isinstance(html, str):
            html = html.encode(DEFAULT_ENCODING)

        pq = PyQuery(html)
        super(HTML, self).__init__(
            element=pq('html') or pq.wrapAll('<html></html>')('html'),
            html=html,
            url=url,
            default_encoding=default_encoding
        )
        self.session = session or async_ and AsyncHTMLSession() or HTMLSession()
        self.page = None
        self.next_symbol = DEFAULT_NEXT_SYMBOL

    def __repr__(self) -> str:
        return f"<HTML url={self.url!r}>"

    def next(self, fetch: bool = False, next_symbol: _NextSymbol = None) -> _Next:
        """Attempts to find the next page, if there is one. If ``fetch``
        is ``True`` (default), returns :class:`HTML <HTML>` object of
        next page. If ``fetch`` is ``False``, simply returns the next URL.

        """
        if next_symbol is None:
            next_symbol = DEFAULT_NEXT_SYMBOL

        def get_next():
            candidates = self.find('a', containing=next_symbol)

            for candidate in candidates:
                if candidate.attrs.get('href'):
                    # Support 'next' rel (e.g. reddit).
                    if 'next' in candidate.attrs.get('rel', []):
                        return candidate.attrs['href']

                    # Support 'next' in classnames.
                    for _class in candidate.attrs.get('class', []):
                        if 'next' in _class:
                            return candidate.attrs['href']

                    if 'page' in candidate.attrs['href']:
                        return candidate.attrs['href']

            try:
                # Resort to the last candidate.
                return candidates[-1].attrs['href']
            except IndexError:
                return None

        __next = get_next()
        if __next:
            url = self._make_absolute(__next)
        else:
            return None

        if fetch:
            return self.session.get(url)
        else:
            return url

    def __iter__(self):

        next = self

        while True:
            yield next
            try:
                next = next.next(fetch=True, next_symbol=self.next_symbol).html
            except AttributeError:
                break

    def __next__(self):
        return self.next(fetch=True, next_symbol=self.next_symbol).html

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            url = self.next(fetch=False, next_symbol=self.next_symbol)
            if not url:
                break
            response = await self.session.get(url)
            return response.html

    def add_next_symbol(self, next_symbol):
        self.next_symbol.append(next_symbol)

    async def _async_render(self, *, url: str, script: str = None, scrolldown, sleep: int, wait: float, reload,
                            content: Optional[str], timeout: Union[float, int], keep_page: bool, cookies: list = [{}]):
        """ Handle page creation and js rendering. Internal use for render/arender methods. """
        try:
            page = await self.browser.new_page()

            # Wait before rendering the page, to prevent timeouts.
            await asyncio.sleep(wait)

            #if cookies:
            #    for cookie in cookies:
            #        if cookie:
            #            await page.setCookie(cookie)

            # Load the given page (GET request, obviously.)
            if reload:
                await page.goto(url, timeout=timeout*1000)
            else:
                await page.goto(f'data:text/html,{self.html}', timeout=timeout*1000)

            result = None
            if script:
                result = await page.evaluate(script)

            if scrolldown:
                for _ in range(scrolldown):
                    await page.keyboard.press('PageDown')
                    await asyncio.sleep(sleep)
            else:
                await asyncio.sleep(sleep)

            if scrolldown:
                await page.keyboard.press('PageDown')

            # Return the content of the page, JavaScript evaluated.
            content = await page.content()
            if not keep_page:
                await page.close()
                page = None
            return content, result, page
        except TimeoutError:
            await page.close()
            page = None
            return None

    def _convert_cookiejar_to_render(self, session_cookiejar):
        """
        Convert HTMLSession.cookies:cookiejar[] for browser.newPage().setCookie
        """
        # |  setCookie(self, *cookies:dict) -> None
        # |      Set cookies.
        # |
        # |      ``cookies`` should be dictionaries which contain these fields:
        # |
        # |      * ``name`` (str): **required**
        # |      * ``value`` (str): **required**
        # |      * ``url`` (str)
        # |      * ``domain`` (str)
        # |      * ``path`` (str)
        # |      * ``expires`` (number): Unix time in seconds
        # |      * ``httpOnly`` (bool)
        # |      * ``secure`` (bool)
        # |      * ``sameSite`` (str): ``'Strict'`` or ``'Lax'``
        cookie_render = {}

        def __convert(cookiejar, key):
            try:
                v = eval("cookiejar." + key)
                if not v:
                    kv = ''
                else:
                    kv = {key: v}
            except:
                kv = ''
            return kv

        keys = [
            'name',
            'value',
            'url',
            'domain',
            'path',
            'sameSite',
            'expires',
            'httpOnly',
            'secure',
        ]
        for key in keys:
            cookie_render.update(__convert(session_cookiejar, key))
        return cookie_render

    def _convert_cookiesjar_to_render(self):
        """
        Convert HTMLSession.cookies for browser.newPage().setCookie
        Return a list of dict
        """
        cookies_render = []
        if isinstance(self.session.cookies, http.cookiejar.CookieJar):
            for cookie in self.session.cookies:
                cookies_render.append(self._convert_cookiejar_to_render(cookie))
        return cookies_render

    def render(self, retries: int = 8, script: str = None, wait: float = 0.2, scrolldown=False, sleep: int = 0,
               reload: bool = True, timeout: Union[float, int] = 8.0, keep_page: bool = False, cookies: list = [{}],
               send_cookies_session: bool = False):
        """Reloads the response in Firefox, and replaces HTML content
        with an updated version, with JavaScript executed.

        :param retries: The number of times to retry loading the page in Firefox.
        :param script: JavaScript to execute upon page load (optional).
        :param wait: The number of seconds to wait before loading the page, preventing timeouts (optional).
        :param scrolldown: Integer, if provided, of how many times to page down.
        :param sleep: Integer, if provided, of how many seconds to sleep after initial render.
        :param reload: If ``False``, content will not be loaded from the browser, but will be provided from memory.
        :param keep_page: If ``True`` will allow you to interact with the browser page through ``r.html.page``.

        :param send_cookies_session: If ``True`` send ``HTMLSession.cookies`` convert.
        :param cookies: If not ``empty`` send ``cookies``.

        If ``scrolldown`` is specified, the page will scrolldown the specified
        number of times, after sleeping the specified amount of time
        (e.g. ``scrolldown=10, sleep=1``).

        If just ``sleep`` is provided, the rendering will wait *n* seconds, before
        returning.

        If ``script`` is specified, it will execute the provided JavaScript at
        runtime. Example:

        .. code-block:: python

            script = \"\"\"
                () => {
                    return {
                        width: document.documentElement.clientWidth,
                        height: document.documentElement.clientHeight,
                        deviceScaleFactor: window.devicePixelRatio,
                    }
                }
            \"\"\"

        Returns the return value of the executed  ``script``, if any is provided:

        .. code-block:: python

            >>> r.html.render(script=script)
            {'width': 800, 'height': 600, 'deviceScaleFactor': 1}

        """

        self.browser = self.session.browser  # Automatically create a event loop and browser
        content = None

        # Automatically set Reload to False, if example URL is being used.
        if self.url == DEFAULT_URL:
            reload = False

        if send_cookies_session:
            cookies = self._convert_cookiesjar_to_render()

        for i in range(retries):
            if not content:
                try:
                    content, result, page_info = self.session._execute_render(
                        url=self.url,  # Or pass specific url if needed
                        script=script,
                        sleep=sleep,
                        wait=wait,
                        content=self.html,  # Or pass specific content
                        reload=reload,
                        scrolldown=scrolldown,
                        timeout=timeout,
                        keep_page=keep_page,
                        cookies=cookies
                    )
                except TypeError as e:
                    pass
            else:
                break

        if not content:
            raise MaxRetries("Unable to render the page. Try increasing timeout")

        html = HTML(url=self.url, html=content.encode(DEFAULT_ENCODING), default_encoding=DEFAULT_ENCODING)
        self.__dict__.update(html.__dict__)
        self.page = page
        return result

    async def arender(self, retries: int = 8, script: str = None, wait: float = 0.2, scrolldown=False, sleep: int = 0,
                      reload: bool = True, timeout: Union[float, int] = 8.0, keep_page: bool = False,
                      cookies: list = [{}], send_cookies_session: bool = False):
        """ Async version of render. Takes same parameters. """

        self.browser = await self.session.browser
        content = None

        # Automatically set Reload to False, if example URL is being used.
        if self.url == DEFAULT_URL:
            reload = False

        if send_cookies_session:
            cookies = self._convert_cookiesjar_to_render()

        for _ in range(retries):
            if not content:
                try:

                    content, result, page = await self._async_render(url=self.url, script=script, sleep=sleep,
                                                                     wait=wait, content=self.html, reload=reload,
                                                                     scrolldown=scrolldown, timeout=timeout,
                                                                     keep_page=keep_page, cookies=cookies)
                except TypeError:
                    pass
            else:
                break

        if not content:
            raise MaxRetries("Unable to render the page. Try increasing timeout")

        html = HTML(url=self.url, html=content.encode(DEFAULT_ENCODING), default_encoding=DEFAULT_ENCODING)
        self.__dict__.update(html.__dict__)
        self.page = page
        return result


class HTMLResponse(requests.Response):
    """An HTML-enabled :class:`requests.Response <requests.Response>` object.
    Effectively the same, but with an intelligent ``.html`` property added.
    """

    def __init__(self, session: Union['HTMLSession', 'AsyncHTMLSession']) -> None:
        super(HTMLResponse, self).__init__()
        self._html = None  # type: HTML
        self.session = session

    @property
    def html(self) -> HTML:
        if not self._html:
            self._html = HTML(session=self.session, url=self.url, html=self.content, default_encoding=self.encoding)

        return self._html

    @classmethod
    def _from_response(cls, response, session: Union['HTMLSession', 'AsyncHTMLSession']):
        html_r = cls(session=session)
        html_r.__dict__.update(response.__dict__)
        return html_r


def user_agent(style=None) -> _UserAgent:
    """Returns an apparently legit user-agent, if not requested one of a specific
    style. Defaults to a Firefox-style User-Agent.
    """
    global useragent
    if (not useragent) and style:
        useragent = UserAgent()

    return useragent[style] if style else DEFAULT_USER_AGENT


def _get_first_or_list(l, first=False):
    if first:
        try:
            return l[0]
        except IndexError:
            return None
    else:
        return l


class BaseSession(requests.Session):
    """ Base Session with async Playwright browser capabilities. """

    def __init__(self, mock_browser: bool = True, verify: bool = True,
                 browser_args: list = ['--no-sandbox'], playwright_args: dict = None):
        super().__init__()
        if mock_browser:
            self.headers['User-Agent'] = user_agent()

        # Response hook might need adjustment if HTMLResponse structure changes
        self.hooks['response'].append(self.response_hook)
        self.verify = verify
        self.__browser_args = browser_args
        self.__playwright_args = playwright_args or {}
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._keep_page_objects = {}  # For handling keep_page if needed

    def response_hook(self, response, **kwargs):
        """ Change response encoding and replace it by a HTMLResponse. """
        # Make sure HTMLResponse exists and is compatible
        if hasattr(response, 'encoding') and not response.encoding:
            response.encoding = DEFAULT_ENCODING
        return HTMLResponse._from_response(response, self)

    async def _ensure_browser_launched(self):
        """Async: Launches Playwright and the browser if not already done."""

        if self._browser and self._browser.is_connected():
            return

        if not self._playwright:
            print("Starting Playwright...")
            self._playwright = await async_playwright().start()
            print("Playwright started.")

        try:
            if self._browser and not self._browser.is_connected():
                print("Browser disconnected, closing old instance...")
                await self._browser.close()
                self._browser = None

            if not self._browser:
                print(f"Launching Firefox browser with args: {self.__browser_args}...")
                launch_kwargs = {
                    'headless': True,
                    'args': self.__browser_args,
                    # ignore_https_errors=not self.verify, # Add back if needed
                }
                launch_kwargs.update(self.__playwright_args)
                self._browser = await self._playwright.firefox.launch(**launch_kwargs)
                print(f"Browser launched: {self._browser.version}")
        except Exception as e:
            print(f"Error launching browser: {e}")
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
            raise

    @property
    async def browser(self) -> Browser:
        """Async Property: Provides the Playwright browser instance."""
        await self._ensure_browser_launched()
        if not self._browser:
            raise RuntimeError("Failed to initialize Playwright browser.")
        return self._browser

    # This is the core ASYNC rendering logic using Playwright
    async def _async_render(self, url: str, script: str | None, sleep: int, wait: float, content: str | None,
                            reload: bool, scrolldown: int | bool, timeout: float, keep_page: bool,
                            cookies: list | None) -> tuple[str, any, dict]:
        """Async: Performs the actual browser rendering using Playwright."""
        browser_instance = await self.browser  # Ensure browser is ready and get it
        page = None
        context = None
        page_key = url or id(content)  # Simple key for keep_page

        try:
            # Use existing context/page if keep_page was used previously for this URL/content
            if keep_page and page_key in self._keep_page_objects:
                page = self._keep_page_objects[page_key]['page']
                context = self._keep_page_objects[page_key]['context']
                print(f"Reusing kept page for {page_key}")
                # Reset potential dialogs or state issues from previous use
                # await page.reload() # Optional: Force reload on reuse?

            else:
                # Create a new context for isolation (recommended)
                context = await browser_instance.new_context(ignore_https_errors=not self.verify)
                if cookies:
                    await context.add_cookies(cookies)  # Playwright format: list of dicts

                page = await context.new_page()

            await page.set_default_timeout(timeout * 1000)  # Playwright uses milliseconds

            # Navigation / Content Loading
            if content and not reload:  # Prioritize content if provided and not reloading
                print(f"Setting content ({len(content)} bytes)...")
                await page.set_content(content)
                # Wait for load state if needed after set_content
                await page.wait_for_load_state('domcontentloaded')
            elif url:
                print(f"Navigating to {url}...")
                await page.goto(url, wait_until='domcontentloaded')  # or 'load' or 'networkidle'
            elif not content and not url and not page.url == 'about:blank':
                # If no url/content provided, maybe reload the existing page if reused?
                print("Reloading current page...")
                await page.reload(wait_until='domcontentloaded')
            elif not content and not url:
                print("Warning: No URL or content provided for rendering.")
                return await page.content(), None, {"url": page.url, "title": await page.title()}

            # Handle 'reload' after initial load (only makes sense if loaded via URL)
            if reload and url and not content:
                print("Reloading page...")
                await page.reload(wait_until='domcontentloaded')

            # Handle 'sleep' (fixed delay)
            if sleep > 0:
                print(f"Sleeping for {sleep} seconds...")
                await page.wait_for_timeout(sleep * 1000)

            # Handle 'scrolldown'
            if scrolldown:
                print(f"Scrolling down {scrolldown} times...")
                scroll_count = scrolldown if isinstance(scrolldown, int) else 1  # Default to 1 if True
                for i in range(scroll_count):
                    await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                    await page.wait_for_timeout(500)  # Wait for potential dynamic content loading
                    print(f"Scroll {i + 1}/{scroll_count} complete.")

            # Handle 'script' execution (JavaScript)
            result = None
            if script:
                print("Executing script...")
                result = await page.evaluate(script)
                print("Script executed.")

            # Handle 'wait' (Interpreting as additional fixed wait after actions)
            # Playwright's automatic waits are usually better.
            # If 'wait' meant "wait for element" or "wait for network", use page.wait_for_selector etc.
            if wait > 0:
                print(f"Waiting for {wait} seconds...")
                await page.wait_for_timeout(wait * 1000)

            print("Retrieving page content...")
            new_content = await page.content()
            page_info = {"url": page.url, "title": await page.title()}
            print("Content retrieved.")

            if keep_page:
                print(f"Keeping page/context for key: {page_key}")
                # Store page and context; potentially overwrite previous kept page for same key
                self._keep_page_objects[page_key] = {'page': page, 'context': context}
            else:
                # Clean up kept page if it existed but keep_page is now false
                if page_key in self._keep_page_objects:
                    print(f"Cleaning up kept page for key: {page_key}")
                    kp_page = self._keep_page_objects[page_key]['page']
                    kp_context = self._keep_page_objects[page_key]['context']
                    if page == kp_page:  # Ensure we are closing the correct one
                        if kp_page and not kp_page.is_closed(): await kp_page.close()
                        if kp_context and kp_context.pages:
                            pass  # Context closes when last page does? Check docs. Or await kp_context.close()?
                        else:
                            await kp_context.close()  # Close context if it has no other pages
                    del self._keep_page_objects[page_key]

            return new_content, result, page_info

        except Exception as e:
            print(f"Error during async render: {e}")
            # Ensure cleanup even on error if not keeping page
            raise  # Re-raise the exception
        finally:
            # Close the page and context ONLY if not keeping them
            if not keep_page:
                if page and not page.is_closed():
                    print("Closing page...")
                    await page.close()
                if context:  # Context manages pages, closing context closes its pages
                    print("Closing context...")
                    await context.close()
                    print("Context closed.")

    async def close(self):
        """Async: Closes the browser, kept pages/contexts, and stops Playwright."""
        print("Closing Playwright Session...")

        # Close any remaining kept pages/contexts
        print(f"Closing {len(self._keep_page_objects)} kept page(s)/context(s)...")
        for key, item in list(self._keep_page_objects.items()):
            page = item.get('page')
            context = item.get('context')
            try:
                if page and not page.is_closed(): await page.close()
                if context: await context.close()  # Closing context handles its pages
            except Exception as e:
                print(f"Error closing kept page/context for key {key}: {e}")
        self._keep_page_objects.clear()

        # Close the main browser instance
        if hasattr(self, "_browser") and self._browser:
            try:
                print("Closing main browser instance...")
                await self._browser.close()
                print("Browser closed.")
            except Exception as e:
                print(f"Error closing browser: {e}")
            self._browser = None

        # Stop the Playwright instance
        if hasattr(self, "_playwright") and self._playwright:
            try:
                print("Stopping Playwright instance...")
                await self._playwright.stop()
                print("Playwright instance stopped.")
            except Exception as e:
                print(f"Error stopping Playwright: {e}")
            self._playwright = None

        # Call requests.Session.close()
        super().close()
        print("Playwright Session closed.")


class HTMLSession(BaseSession):
    """ Synchronous interface using the async Playwright BaseSession. """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # No loop management needed here anymore, asyncio.run handles it.

    @property
    def browser(self):
        """Synchronous Property: Gets the browser, launching if needed via asyncio.run."""
        # Check if already available and connected (synchronously where possible)
        # Note: self._browser might exist but be disconnected. `is_connected()` check needs async.
        # Simplest is just to run the async property getter every time.
        # More efficient might store a sync flag, but adds complexity.
        if not (hasattr(self, "_browser") and self._browser):
            print("HTMLSession: Browser not cached, running async getter...")
            try:
                # Run the async property getter using asyncio.run
                # This handles loop creation/destruction for this call.
                _ = asyncio.run(super().browser)  # Call async property, result stored in self._browser
            except RuntimeError as e:
                if "cannot be called from a running event loop" in str(e):
                    raise RuntimeError(
                        "Cannot initialize browser from within an existing asyncio event loop using HTMLSession. "
                        "Use an asynchronous session/client instead."
                    ) from e
                else:
                    raise
            print("HTMLSession: Browser ready.")
        # The async property call above ensures self._browser is set
        return self._browser

    def close(self):
        """ Synchronously close the browser and Playwright instance. """
        print("HTMLSession: Closing session...")
        try:
            # Run the async close method from BaseSession
            asyncio.run(super().close())
        except RuntimeError as e:
            if "cannot be called from a running event loop" in str(e):
                # Less critical to raise here, but indicates potential issue if called from async code
                print("Warning: close() called from within an existing event loop.")
                # Attempt manual cleanup if possible/needed, or just log
            else:
                raise  # Re-raise other runtime errors
        except Exception as e:
            print(f"Error during HTMLSession close: {e}")
        # Ensure requests.Session.close is called even if BaseSession async close fails
        # super(BaseSession, self).close() # Call parent's close if BaseSession didn't already

    # New method to bridge sync call to async render logic
    def _execute_render(self, **kwargs):
        """
        Synchronous method that ensures the browser is ready and then
        runs the async rendering logic using asyncio.run.
        """
        print("HTMLSession: Executing render...")
        # Ensure browser is launched by accessing the property.
        # The property itself now uses asyncio.run if needed.
        _ = self.browser  # This ensures self._browser is populated

        # Now, run the actual async rendering task from BaseSession
        try:
            return asyncio.run(self._async_render(**kwargs))
        except RuntimeError as e:
            if "cannot be called from a running event loop" in str(e):
                raise RuntimeError(
                    "render() cannot be used inside an existing asyncio event loop. "
                    "Use an asynchronous session/client or ensure render() is called from a purely synchronous context."
                ) from e
            else:
                raise


class AsyncHTMLSession(BaseSession):
    """ An async consumable session. """

    def __init__(self, loop=None, workers=None,
                 mock_browser: bool = True, *args, **kwargs):
        """ Set or create an event loop and a thread pool.

            :param loop: Asyncio loop to use.
            :param workers: Amount of threads to use for executing async calls.
                If not pass it will default to the number of processors on the
                machine, multiplied by 5. """
        super().__init__(*args, **kwargs)

        self.loop = loop or asyncio.get_event_loop()
        self.thread_pool = ThreadPoolExecutor(max_workers=workers)

    def request(self, *args, **kwargs):
        """ Partial original request func and run it in a thread. """
        func = partial(super().request, *args, **kwargs)
        return self.loop.run_in_executor(self.thread_pool, func)

    async def close(self):
        """ If a browser was created close it first. """
        if hasattr(self, "_browser"):
            await self._browser.close()
        await super().close()

    def run(self, *coros):
        """ Pass in all the coroutines you want to run, it will wrap each one
            in a task, run it and wait for the result. Return a list with all
            results, this is returned in the same order coros are passed in. """
        tasks = [
            asyncio.ensure_future(coro()) for coro in coros
        ]
        done, _ = self.loop.run_until_complete(asyncio.wait(tasks))
        return [t.result() for t in done]
