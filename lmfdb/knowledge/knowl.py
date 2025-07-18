# the basic knowledge object, with database awareness, …

from collections import defaultdict
from datetime import timedelta
from lmfdb.utils.datetime_utils import utc_now_naive, ensure_naive_utc, datetime_to_timestamp_in_ms
import re
import subprocess
import time

from psycodict.base import PostgresBase
from psycodict import DelayCommit
from lmfdb import db
from lmfdb.app import is_beta
from lmfdb.utils import code_snippet_knowl
from lmfdb.utils.config import Configuration
from lmfdb.users.pwdmanager import userdb
from psycopg2.sql import SQL, Identifier, Placeholder
from sage.all import cached_function
from lmfdb.knowledge import logger

# Timezone handling utilities for knowl system
#
# IMPORTANT: The knowl database stores timestamps in columns defined as
# "timestamp without time zone". All timestamps are stored as UTC but
# without timezone information. These utilities ensure consistent handling.


text_keywords = re.compile(r"\b[a-zA-Z0-9-]{3,}\b")
top_knowl_re = re.compile(r"(.*)\.top$")
comment_knowl_re = re.compile(r"(.*)\.(\d+)\.comment$")
coldesc_knowl_re = re.compile(r"columns.([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)")
tabledesc_knowl_re = re.compile(r"tables.([A-Za-z0-9_]+)")
bottom_knowl_re = re.compile(r"(.*)\.bottom$")
url_from_knowl = [
    (re.compile(r'g2c\.(\d+\.[a-z]+\.\d+\.\d+)'), 'Genus2Curve/Q/{0}', 'Genus 2 curve {0}'),
    (re.compile(r'g2c\.(\d+\.[a-z]+)'), 'Genus2Curve/Q/{0}', 'Genus 2 isogeny class {0}'),
    (re.compile(r'gg\.(\d+)t(\d+)'), 'GaloisGroup/{0}t{1}', 'Galois group {0}T{1}'),
    (re.compile(r'lattice\.(.*)'), 'Lattice/{0}', 'Lattice {0}'),
    (re.compile(r'cmf\.(.*)'), 'ModularForm/GL2/Q/holomorphic/{0}', 'Newform {0}'),
    (re.compile(r'nf\.(.*)'), 'NumberField/{0}', 'Number field {0}'),
    (re.compile(r'ec\.q\.(.*)'), 'EllipticCurve/Q/{0}', 'Elliptic curve {0}'),
    (re.compile(r'ec\.(\d+\.\d+\.\d+\.\d+)-(\d+\.\d+)-([a-z]+)(\d+)'), 'EllipticCurve/{0}/{1}/{2}/{3}', 'Elliptic curve {0}-{1}-{2}{3}'),
    (re.compile(r'av\.fq\.(.*)'), 'Variety/Abelian/Fq/{0}', 'Abelian variety isogeny class {0}'),
    (re.compile(r'st_group\.(.*)'), 'SatoTateGroup/{0}', 'Sato-Tate group {0}'),
    (re.compile(r'belyi\.(.*)'), 'Belyi/{0}', 'Belyi Map {0}'),
    (re.compile(r'hecke_algebra\.(.*)'), 'ModularForm/GL2/Q/HeckeAlgebra/{0}', 'Hecke algebra {0}'),
    (re.compile(r'hecke_algebra_l_adic\.(.*)'), 'ModularForm/GL2/Q/HeckeAlgebra/{0}/2', 'l-adic Hecke algebra {0}'),
    (re.compile(r'gal\.modl\.(.*)'), 'Representation/Galois/ModL/{0}', 'Mod-l Galois representation {0}'),
    (re.compile(r'modlmf\.(.*)'), 'ModularForm/GL2/ModL/{0}', 'Mod-l modular form {0}'),
    (re.compile(r'group\.abstract\.(.*)'), 'Groups/Abstract/{0}', 'Abstract group {0}'),
]
grep_extractor = re.compile(r'(.+?)([:|-])(\d+)([-|:])(.*)')
# We need to convert knowl
link_finder_re = re.compile(r"""(KNOWL(_INC)?\(|kid\s*=|knowl\s*=|th_wrap\s*\()\s*['"]([^'"]+)['"]|""")
define_fixer = re.compile(r"""\{\{\s*KNOWL(_INC)?\s*\(\s*['"]([^'"]+)['"]\s*,\s*(title\s*=\s*)?([']([^']+)[']|["]([^"]+)["]\s*)\)\s*\}\}""")
defines_finder_re = re.compile(r"""\*\*([^\*]+)\*\*""")
# this one is different from the hashtag regex in main.py,
# because of the match-group ( ... )
hashtag_keywords = re.compile(r'#[a-zA-Z][a-zA-Z0-9-_]{1,}\b')
common_words = {'and', 'an', 'or', 'some', 'many', 'has', 'have', 'not', 'too', 'mathbb', 'title', 'for'}

# categories, level 0, never change this id
#CAT_ID = 'categories'

def make_keywords(content, kid, title):
    """
    this function is used to create the keywords for the
    full text search. tokenizes them and returns a list
    of the id, the title and content string.
    """
    kws = [kid]  # always included
    kws += kid.split(".")
    kws += text_keywords.findall(title)
    kws += text_keywords.findall(content)
    kws += hashtag_keywords.findall(title)
    kws += hashtag_keywords.findall(content)
    kws = [k.lower() for k in kws]
    kws = set(kws)
    return [w for w in kws if w not in common_words]


def extract_cat(kid):
    if not hasattr(kid, 'split'):
        return None
    return kid.split(".")[0]

def extract_typ(kid):
    m = comment_knowl_re.match(kid)
    if m:
        typ = -2
        source = m.group(1)
        return typ, source, None
    m = coldesc_knowl_re.match(kid)
    if m:
        return 2, m.group(1), m.group(2)
    m = tabledesc_knowl_re.match(kid)
    if m:
        return 2, None, m.group(1)
    m = top_knowl_re.match(kid)
    if m:
        prelabel = m.group(1)
        typ = 1
    else:
        m = bottom_knowl_re.match(kid)
        if m:
            prelabel = m.group(1)
            typ = -1
        else:
            return 0, None, None
    url = None
    name = None
    for matcher, url_pattern, name_pattern in url_from_knowl:
        m = matcher.match(prelabel)
        if m:
            url = url_pattern.format(*m.groups())
            name = name_pattern.format(*m.groups())
            break
    return typ, url, name


def extract_links(content):
    return sorted({x[2] for x in link_finder_re.findall(content) if x[2]})


def normalize_define(term):
    m = define_fixer.search(term)
    if m:
        n = 6 if (m.group(5) is None) else 5
        term = define_fixer.sub(r'\%s' % n, term)
    return ' '.join(term.lower().replace('"', '').replace("'", "").split())


def extract_defines(content):
    return sorted({x.strip() for x in defines_finder_re.findall(content)})

# We don't use the PostgresTable from psycodict.database
# since it's aimed at constructing queries for mathematical objects


class KnowlBackend(PostgresBase):
    _default_fields = ['authors', 'cat', 'content', 'last_author', 'timestamp', 'title', 'status', 'type', 'links', 'defines', 'source', 'source_name'] # doesn't include id, _keywords, reviewer or review_timestamp

    def __init__(self):
        PostgresBase.__init__(self, 'db_knowl', db)
        self._rw_knowldb = db.can_read_write_knowls()
        # we cache knowl titles for 10s
        self.caching_time = 10
        self.cached_titles_timestamp = 0
        self.cached_defines_timestamp = 0
        self.cached_titles = {}

    def _safe_execute(self, query, values=None):
        # Every 20 minutes we reload the knowl database on production
        # using a dump from beta.  If this query is run during the time
        # that restore happens, we could trigger an error.  The restore
        # takes about 0.6 seconds, so if we hit an error we wait
        # 1 second and try again.
        try:
            return list(self._execute(query, values))
        except Exception:
            time.sleep(1)
            return list(self._execute(query, values))

    @property
    def titles(self):
        now = time.time()
        if now - self.cached_titles_timestamp > self.caching_time:
            self.cached_titles_timestamp = now
            self.cached_titles = {elt['id']: elt['title'] for elt in self.get_all_knowls(['id','title'])}
        return self.cached_titles

    @property
    def all_defines(self):
        now = time.time()
        if now - self.cached_defines_timestamp > self.caching_time:
            self.cached_defines_timestamp = now
            self.cached_defines = defaultdict(list)
            for elt in self.get_all_defines():
                for term in elt['defines']:
                    self.cached_defines[term].append(elt['id'])
        return self.cached_defines

    def can_read_write_knowls(self):
        return self._rw_knowldb

    def get_knowl(self, ID,
            fields=None, beta=None, allow_deleted=False, timestamp=None):
        if fields is None:
            fields = ['id'] + self._default_fields
        if timestamp is not None:
            timestamp = ensure_naive_utc(timestamp)
            logger.debug("Fetching knowl with ID: %s and timestamp: %s", ID, timestamp)
            selecter = SQL("SELECT {0} FROM kwl_knowls WHERE id = %s AND timestamp = %s LIMIT 1").format(SQL(", ").join(map(Identifier, fields)))
            L = self._safe_execute(selecter, [ID, timestamp])
            if L:
                return dict(zip(fields, L[0]))
            else:
                return None

        if beta is None:
            beta = is_beta()
        selecter = SQL("SELECT {0} FROM kwl_knowls WHERE id = %s AND status >= %s ORDER BY timestamp DESC LIMIT 1").format(SQL(", ").join(map(Identifier, fields)))
        if not beta:
            L = self._safe_execute(selecter, [ID, 1])
            if L:
                return dict(zip(fields, L[0]))
        L = self._safe_execute(selecter, [ID, -2 if allow_deleted else 0])
        if L:
            return dict(zip(fields, L[0]))

    def get_all_knowls(self, fields=None, types=[2, 1,0,-1,-2]):
        if fields is None:
            fields = ['id'] + self._default_fields
        selecter = SQL("SELECT DISTINCT ON (id) {0} FROM kwl_knowls WHERE status >= %s AND type = ANY(%s) ORDER BY id, timestamp DESC").format(SQL(", ").join(map(Identifier, fields)))
        L = self._safe_execute(selecter, [0, types])
        return [dict(zip(fields, res)) for res in L]

    def get_all_defines(self):
        selecter = SQL("SELECT DISTINCT ON (id) id, defines FROM kwl_knowls WHERE status >= 0 AND type = 0 AND cardinality(defines) > 0 ORDER BY id, timestamp DESC")
        L = self._safe_execute(selecter)
        # This should be fixed in the data
        return [{k: (v if k == 'id' else [normalize_define(t) for t in v])
                 for k, v in zip(['id', 'defines'], res)} for res in L]

    #FIXME shouldn't I be allowed to search on id? or something?
    def search(self, category="", filters=[], types=[], keywords="", author=None, sort=[], projection=['id', 'title'], regex=False):
        """
        INPUT:

        - ``category`` -- a knowl category such as "ec"  or "mf".
        - ``filters`` -- a list, giving a subset of "beta", "reviewed", "in progress" and "deleted".
            Knowls in the returned list will have their most recent status among the provided values.
        - ``types`` -- a list, giving a subset of ["normal", "annotations"]
        - ``keywords`` -- a string giving a space separated list of lower case keywords from the id, title and content.  If regex is set, will be used instead as a regular expression to match against content, title and knowl id.
        - ``author`` -- a string or list of strings giving authors
        - ``sort`` -- a list of strings or pairs (x, dir) where x is a column name and dir is 1 or -1.
        - ``projection`` -- a list of column names, not including ``_keywords``
        - ``regex`` -- whether to use regular expressions rather than keyword search
        """
        restrictions = []
        values = []
        if 'in progress' not in filters:
            restrictions.append(SQL("status != %s"))
            values.append(-1)
        # In order to be able to sort by arbitrary columns, we have to select everything here.
        # We therefore do the projection in Python, which is fine for the knowls table since it's tiny
        fields = ['id', '_keywords'] + self._default_fields
        sqlfields = SQL(", ").join(map(Identifier, fields))
        projfields = [(col, fields.index(col)) for col in projection]
        if restrictions:
            restrictions = SQL(" WHERE ") + SQL(" AND ").join(restrictions)
        else:
            restrictions = SQL("")
        selecter = SQL("SELECT DISTINCT ON (id) {0} FROM kwl_knowls{1} ORDER BY id, timestamp DESC").format(sqlfields, restrictions)
        secondary_restrictions = []
        if filters:
            secondary_restrictions.append(SQL("knowls.{0} = ANY(%s)").format(Identifier("status")))
            values.append([knowl_status_code[q] for q in filters if q in knowl_status_code])
        else:
            secondary_restrictions.append(SQL("status >= %s"))
            values.append(0)
        if category:
            secondary_restrictions.append(SQL("cat = %s"))
            values.append(category)
        if keywords:
            if regex:
                secondary_restrictions.append(SQL("content ~ %s OR title ~ %s OR id ~ %s"))
                values.extend([keywords, keywords, keywords])
            else:
                keywords = [w for w in keywords.split(" ") if len(w) >= 3]
                if keywords:
                    secondary_restrictions.append(SQL("_keywords @> %s"))
                    values.append(keywords)
        if author is not None:
            secondary_restrictions.append(SQL("authors @> %s"))
            values.append([author])
        if not types:
            # default to just showing normal knowls
            types = ["normal"]
        if len(types) == 1:
            secondary_restrictions.append(SQL("type = %s"))
            values.append(knowl_type_code[types[0]])
        else:
            secondary_restrictions.append(SQL("type = ANY(%s)"))
            values.append([knowl_type_code[typ] for typ in types])
        secondary_restrictions = SQL(" AND ").join(secondary_restrictions)
        if sort:
            sort = SQL(" ORDER BY ") + self._sort_str(sort)
        else:
            sort = SQL("")
        selecter = SQL("SELECT {0} FROM ({1}) knowls WHERE {2}{3}").format(sqlfields, selecter, secondary_restrictions, sort)
        L = self._safe_execute(selecter, values)
        return [{k:res[i] for k,i in projfields} for res in L]

    def save(self, knowl, who, most_recent=None, minor=False):
        """who is the ID of the user, who wants to save the knowl"""
        if most_recent is None:
            most_recent = self.get_knowl(knowl.id, ['id'] + self._default_fields, allow_deleted=False)
        new_knowl = most_recent is None
        if new_knowl:
            authors = []
        else:
            authors = most_recent.pop('authors', [])

        if not minor and who and who not in authors:
            authors = authors + [who]

        search_keywords = make_keywords(knowl.content, knowl.id, knowl.title)
        cat = extract_cat(knowl.id)
        # When renaming, source is set explicitly on the knowl
        if knowl.type == 0 and knowl.source is not None:
            typ, source, name = 0, knowl.source, knowl.source_name
        else:
            typ, source, name = extract_typ(knowl.id)
        links = extract_links(knowl.content)
        if typ == 2: # column or table description
            defines = [name]
        else:
            defines = extract_defines(knowl.content)
        # id, authors, cat, content, last_author, timestamp, title, status, type, links, defines, source, source_name
        values = (knowl.id, authors, cat, knowl.content, who, knowl.timestamp, knowl.title, knowl.status, typ, links, defines, source, name, search_keywords)
        with DelayCommit(self):
            inserter = SQL("INSERT INTO kwl_knowls (id, {0}, _keywords) VALUES ({1})")
            inserter = inserter.format(SQL(', ').join(map(Identifier, self._default_fields)), SQL(", ").join(Placeholder() * (len(self._default_fields) + 2)))
            self._execute(inserter, values)
        self.cached_titles[knowl.id] = knowl.title

    def get_history(self, limit=25):
        """
        returns the last @limit history items
        """
        cols = ("id", "title", "timestamp", "last_author")
        selecter = SQL("SELECT {0} FROM kwl_knowls WHERE status >= %s AND type != %s ORDER BY timestamp DESC LIMIT %s").format(SQL(", ").join(map(Identifier, cols)))
        L = self._safe_execute(selecter, [0, -2, limit])
        return [dict(zip(cols, res)) for res in L]

    def get_comment_history(self, limit=25):
        """
        returns the last @limit knowls that have been commented on
        """
        # We want to select the oldest version of each comment but the newest version of each knowl
        selecter = SQL("WITH k AS (SELECT DISTINCT ON (id) id, title, timestamp, last_author FROM kwl_knowls WHERE status >= %s AND type != %s ORDER BY id, timestamp DESC), c AS (SELECT id, timestamp, last_author, source FROM (SELECT DISTINCT ON (id) id, timestamp, last_author, source FROM kwl_knowls WHERE status >= %s AND type = %s ORDER BY id, timestamp) ci ORDER BY timestamp DESC LIMIT %s) SELECT k.id, k.title, k.timestamp, k.last_author, c.id, c.timestamp, c.last_author FROM k, c WHERE k.id = c.source ORDER BY c.timestamp DESC")
        L = self._safe_execute(selecter, [0, -2, 0, -2, limit])
        return [dict(zip(["knowl_id", "knowl_title", "knowl_timestamp", "knowl_author", "comment_id", "comment_timestamp", "comment_author"], res)) for res in L]

    def get_edit_history(self, ID):
        selecter = SQL("SELECT timestamp, last_author, content, status FROM kwl_knowls WHERE status >= %s AND id = %s ORDER BY timestamp")
        L = self._safe_execute(selecter, [0, ID])
        return [dict(zip(["timestamp", "last_author", "content", "status"], rec)) for rec in L]

    def get_comments(self, ID):
        # Note that the subselect is sorted in ascending order by timestamp
        selecter = SQL("SELECT id, last_author, timestamp FROM (SELECT DISTINCT ON (id) id, last_author, timestamp FROM kwl_knowls WHERE type = %s AND source = %s AND status >= 0 ORDER BY id, timestamp) knowls ORDER BY timestamp DESC")
        return self._safe_execute(selecter, [-2, ID])

    def get_column_descriptions(self, table):
        fields = ['id'] + self._default_fields
        selecter = SQL("SELECT {0} FROM (SELECT DISTINCT ON (id) {0} FROM kwl_knowls WHERE id LIKE %s AND type = %s AND status >= %s ORDER BY id, timestamp) knowls ORDER BY id").format(SQL(", ").join(map(Identifier, fields)))
        L = self._safe_execute(selecter, [f"columns.{table}.%", 2, 0])
        return {rec[0].split(".")[-1]: Knowl(rec[0], data=dict(zip(fields, rec))) for rec in L}

    def set_column_description(self, table, col, description):
        uid = db.login()
        kid = f"columns.{table}.{col}"
        data = {
            'content': description,
            'defines': col,
        }
        kwl = Knowl(kid, data=data)
        old = self.get_knowl(kid, beta=True)
        if old is None:
            old = {'authors': []}
        self.save(kwl, uid, most_recent=old)

    def drop_column(self, table, col):
        kid = f"columns.{table}.{col}"
        kwl = Knowl(kid, data=self.get_knowl(kid, beta=True))
        self.delete(kwl)

    def get_table_description(self, table):
        fields = ['id'] + self._default_fields
        selecter = SQL("SELECT {0} FROM (SELECT DISTINCT ON (id) {0} FROM kwl_knowls WHERE id = %s AND type = %s AND status >= %s ORDER BY id, timestamp) knowls ORDER BY id LIMIT 1").format(SQL(", ").join(map(Identifier, fields)))
        L = self._safe_execute(selecter, [f"tables.{table}", 2, 0])
        if L:
            return Knowl(L[0][0], data=dict(zip(fields, L[0])))

    def set_table_description(self, table, description):
        uid = db.login()
        kid = f"tables.{table}"
        data = {
            'content': description,
            'defines': table,
        }
        kwl = Knowl(kid, data=data)
        old = self.get_knowl(kid, beta=True)
        if old is None:
            old = {'authors': []}
        self.save(kwl, uid, most_recent=old)

    def drop_table(self, table):
        kid = f"tables.{table}"
        kwl = Knowl(kid, data=self.get_knowl(kid, beta=True))
        self.delete(kwl)

    def delete(self, knowl):
        """deletes this knowl from the db. This is effected by setting the status to -2 on all copies of the knowl"""
        updator = SQL("UPDATE kwl_knowls SET status=%s WHERE id=%s")
        self._execute(updator, [-2, knowl.id])
        if knowl.id in self.cached_titles:
            self.cached_titles.pop(knowl.id)

    def resurrect(self, knowl):
        """Sets the status for all deleted copies of the knowl to beta"""
        updator = SQL("UPDATE kwl_knowls SET status=%s WHERE status=%s AND id=%s")
        self._execute(updator, [0, -2, knowl.id])
        self.cached_titles[knowl.id] = knowl.title

    def review(self, knowl, who, set_beta=False):
        updator = SQL("UPDATE kwl_knowls SET (status, reviewer, reviewer_timestamp) = (%s, %s, %s) WHERE id = %s AND timestamp = %s")
        self._execute(updator, [0 if set_beta else 1, who, utc_now_naive(), knowl.id, knowl.timestamp])

    def _set_referrers(self, knowls):
        kids = [k.id for k in knowls]
        selecter = SQL("SELECT id, links FROM (SELECT DISTINCT ON (id) id, links FROM kwl_knowls WHERE status >= %s AND type != %s ORDER BY id, timestamp DESC) knowls WHERE links && %s")
        L = self._safe_execute(selecter, [0, -2, kids])
        referrers = {k.id: [] for k in knowls}
        for refid, links in L:
            for kid in links:
                if kid in referrers:
                    referrers[kid].append(refid)
        for k in knowls:
            k.referrers = referrers[k.id]
            k.code_referrers = [
                    code_snippet_knowl(D, full=False)
                    for D in self.code_references(k)]

    def needs_review(self, days):
        now = utc_now_naive()
        tdelta = timedelta(days=days)
        time = now - tdelta
        fields = ['id'] + self._default_fields
        selecter = SQL("SELECT {0} FROM (SELECT DISTINCT ON (id) {0} FROM kwl_knowls WHERE timestamp >= %s AND status >= %s AND type >= -1 AND type <= 1 ORDER BY id, timestamp DESC) knowls WHERE status = 0 ORDER BY timestamp DESC").format(SQL(", ").join(map(Identifier, fields)))
        L = self._safe_execute(selecter, [time, 0])
        knowls = [Knowl(rec[0], data=dict(zip(fields, rec))) for rec in L]

        kids = [k.id for k in knowls]
        selecter = SQL("SELECT DISTINCT ON (id) id, content FROM kwl_knowls WHERE status = 1 AND id = ANY(%s) ORDER BY id, timestamp DESC")
        L = self._safe_execute(selecter, [kids])
        reviewed = {rec[0]:rec[1] for rec in L}

        for k in knowls:
            k.reviewed_content = reviewed.get(k.id)
        self._set_referrers(knowls)
        return knowls

    def stale_knowls(self):
        fields = ['id'] + self._default_fields
        selecter = SQL("SELECT {0}, {1} FROM (SELECT DISTINCT ON (id) {2} FROM kwl_knowls WHERE status = 0 AND type != -2 ORDER BY id, timestamp DESC) a, (SELECT DISTINCT ON (id) {2} FROM kwl_knowls WHERE status = 1 AND type != -2 ORDER BY id, timestamp DESC) b WHERE a.id = b.id AND a.timestamp > b.timestamp ORDER BY a.timestamp").format(
            SQL(", ").join(SQL("a.{0}").format(Identifier(col)) for col in fields),
            SQL(", ").join(SQL("b.{0}").format(Identifier(col)) for col in fields),
            SQL(", ").join(map(Identifier, fields)))
        data = self._safe_execute(selecter)
        knowls = [Knowl(rec[0], data=dict(zip(fields, rec))) for rec in data]
        for knowl, rec in zip(knowls, data):
            D = dict(zip(fields, rec[len(fields):]))
            knowl.reviewed_content = D["content"]
        self._set_referrers(knowls)
        return knowls

    def ids_referencing(self, knowlid, old=False, beta=None):
        """
        Returns all ids that reference the given one.

        Note that if running on prod, and the reviewed version of a knowl doesn't
        reference knowlid but a more recent beta version does, it will be included
        in the results even though the displayed knowl will not include a reference.

        INPUT:

        - ``knowlid`` -- a knowl id in the database
        - ``old`` -- whether to include knowls that used to reference this one, but no longer do.
        - ``beta`` -- if False, use the most recent positively reviewed knowl, rather than the most recent.
        """
        values = [0, -2, [knowlid]]
        if old:
            selecter = SQL("SELECT DISTINCT ON (id) id FROM kwl_knowls WHERE status >= %s AND type != %s AND links @> %s")
        else:
            if beta is None:
                beta = is_beta()
            if not beta:
                # Have to make sure we do display references where the most recent positively reviewed knowl does reference this, but the most recent beta does not.
                selecter = SQL("SELECT id FROM (SELECT DISTINCT ON (id) id, links FROM kwl_knowls WHERE status > %s AND type != %s ORDER BY id, timestamp DESC) knowls WHERE links @> %s")
                L = self._safe_execute(selecter, values)
                good_ids = [rec[0] for rec in L]
                # Have to make sure that we don't display knowls as referencing this one when the most recent positively reviewed knowl doesn't but the most recent beta knowl does.
                selecter = SQL("SELECT id FROM (SELECT DISTINCT ON (id) id, links FROM kwl_knowls WHERE status > %s AND type != %s ORDER BY id, timestamp DESC) knowls WHERE NOT (links @> %s)")
                L = self._safe_execute(selecter, values)
                bad_ids = [rec[0] for rec in L]
            # We also need new knowls that have never been reviewed
            selecter = SQL("SELECT id FROM (SELECT DISTINCT ON (id) id, links FROM kwl_knowls WHERE status >= %s AND type != %s ORDER BY id, timestamp DESC) knowls WHERE links @> %s")
        L = self._safe_execute(selecter, values)
        if not beta and not old:
            new_ids = [rec[0] for rec in L if rec[0] not in bad_ids]
            return sorted(set(new_ids + good_ids))
        else:
            return [rec[0] for rec in L]

    def orphans(self, old=False, beta=None):
        """
        Returns lists of knowl ids (grouped by category) that are not referenced by any code or other knowl.
        """
        kids = {k['id'] for k in self.get_all_knowls(['id'], types=[0]) if not any(k['id'].startswith(x) for x in ["users.", "test."])}

        def filter_from_matches(pattern):
            matches = subprocess.check_output(['git', 'grep', '-E', '--full-name', '--line-number', '--context', '2', pattern],encoding='utf-8').split('\n--\n')
            for match in matches:
                lines = match.split('\n')
                for line in lines:
                    m = grep_extractor.match(line)
                    if m and m.group(2) == ':': # active match rather than context
                        for kid in extract_links(line):
                            if kid in kids:
                                kids.remove(kid)

        # Find references in the codebase
        filter_from_matches(link_finder_re.pattern)
        selecter = SQL("SELECT DISTINCT ON (id) id, links, cat, title FROM kwl_knowls WHERE status >= %s ORDER BY id, timestamp DESC")
        L = self._safe_execute(selecter, [0])
        categories = {}
        titles = {}
        for rec in L:
            categories[rec[0]] = rec[2]
            titles[rec[0]] = rec[3]
            for link in rec[1]:
                if link in kids:
                    kids.remove(link)
        # Some of these might be spurious since they may occur in the code in strange ways, so we do a grep for all of the ids.
        pattern = "|".join(kid.replace(".", r"\.") for kid in kids)
        filter_from_matches(pattern)
        # Now group by category
        by_category = defaultdict(list)
        for kid in kids:
            by_category[categories[kid]].append((titles[kid], kid))
        for cat in by_category:
            L = sorted(by_category[cat])
            by_category[cat] = [kid for title, kid in L]
        return by_category

    @staticmethod
    def _process_git_grep(match):
        line_numbers = []
        filename = None
        code = []
        for line in match.split('\n'):
            if not line.strip() or line.startswith("Binary file "):
                continue
            m = grep_extractor.match(line)
            if not m:
                raise ValueError("Unexpected grep output (no match)")
            F, con1, n, con2, codeline = m.groups()
            if filename is None:
                filename = F
            if con1 != con2:
                raise ValueError("Unexpected grep output (context marker)")
            if filename != F:
                raise ValueError("Unexpected grep output (mismatched filename)")
            if con1 == ':':
                # If multiple lines match close by, we may have multiple line numbers.
                # This should be rare, so we just show the last one
                line_numbers.append(int(n))
            code.append(codeline)
        return {'filename': filename, 'lines': line_numbers, 'code': code}

    def code_references(self, knowl):
        """
        INPUT:

        - ``knowl`` -- a knowl_object

        OUTPUT:

        A list of dictionaries, with keys
        - 'filename' -- full filename from LMFDB root
        - 'line' -- line number of match
        - 'code' -- a list of strings giving two lines of context around the match.
        """
        kids = [knowl.id]
        if knowl.type == 0 and knowl.source is not None and knowl.source != knowl.id:
            # Currently renaming from another name
            kids.append(knowl.source)
        matches = []
        for kid in kids:
            try:
                matches.extend(subprocess.check_output(['git', 'grep', '--full-name', '--line-number', '--context', '2', """['"]%s['"]""" % (kid.replace('.',r'\.'))],encoding='utf-8').split('\n--\n'))
            except subprocess.CalledProcessError:  # no matches
                pass
        return [self._process_git_grep(match) for match in matches]

    def check_sed_safety(self, knowlid):
        """
        OUTPUT:

        - 0 if the knowl is not referenced in lmfdb code
        - 1 if the knowl is referenced and can be safely replaced using sed
            (does not occur without surrounding quotes)
        - -1 if the knowl is referenced but cannot be safely replaced.
        """
        try:
            matches = subprocess.check_output(['git', 'grep', """['"]%s['"]""" % (knowlid.replace('.',r'\.'))],encoding='utf-8').split('\n')
        except subprocess.CalledProcessError:  # no matches
            return 0

        easy_matches = subprocess.check_output(['git', 'grep', knowlid.replace('.',r'\.')],encoding='utf-8').split('\n')
        return 1 if (len(matches) == len(easy_matches)) else -1

    def start_rename(self, knowl, new_name, who):
        """
        Create a copy of knowl in the database with the new name.  Raises a ValueError if
        * the knowl is a comment or annotation
        * there is a renaming that already includes this knowl
        * the new name already exists

        The calling function should check that new_name is an acceptable knowl id.
        """
        if knowl.type != 0:
            raise ValueError("Only normal knowls can be renamed.")
        if knowl.source is not None or knowl.source_name is not None:
            raise ValueError("This knowl is already involved in a rename.  Use undo_rename or actually_rename instead.")
        if self.knowl_exists(new_name):
            raise ValueError("A knowl with id %s already exists." % new_name)
        updater = SQL("UPDATE kwl_knowls SET (source, source_name) = (%s, %s) WHERE id = %s AND timestamp = %s")
        old_name = knowl.id
        with DelayCommit(self):
            self._execute(updater, [old_name, new_name, old_name, knowl.timestamp])
            new_knowl = knowl.copy(ID=new_name, timestamp=utc_now_naive(), source=knowl.id)
            new_knowl.save(who, most_recent=knowl, minor=True)

    def undo_rename(self, knowl):
        """
        INPUT:

        - ``knowl`` -- the knowl with the old, desired name
        """
        if knowl.source_name is None:
            raise ValueError("Knowl renaming has not been started")
        renamed_knowl = Knowl(knowl.source_name)
        self.actually_rename(renamed_knowl, knowl.id)

    def actually_rename(self, knowl, new_name=None):
        if new_name is None:
            new_name = knowl.source_name
            if new_name is None:
                raise ValueError("You must either call start_rename or provide the new name")
        with DelayCommit(self):
            new_cat = extract_cat(new_name)
            updator = SQL("UPDATE kwl_knowls SET (id, cat) = (%s, %s) WHERE id = %s")
            self._execute(updator, [new_name, new_cat, knowl.id])
            # Remove source and source_name from all versions
            updator = SQL("UPDATE kwl_knowls SET (source, source_name) = (%s, %s) WHERE id = %s")
            self._execute(updator, [None, None, new_name])
            # Only have to update keywords for most recent version and most recent reviewed version
            updator = SQL("UPDATE kwl_knowls SET _keywords = %s WHERE id = %s AND timestamp = %s")
            self._execute(updator, [make_keywords(knowl.content, new_name, knowl.title), new_name, knowl.timestamp])
            if knowl.reviewed_timestamp and knowl.reviewed_timestamp != knowl.timestamp:
                self._execute(updator, [make_keywords(knowl.reviewed_content, new_name, knowl.reviewed_title), new_name, knowl.reviewed_timestamp])
            referrers = self.ids_referencing(knowl.id, old=True)
            updator = SQL("UPDATE kwl_knowls SET (content, links) = (regexp_replace(content, %s, %s, %s), array_replace(links, %s, %s)) WHERE id = ANY(%s)")
            values = [r"""['"]\s*{0}\s*['"]""".format(knowl.id.replace('.', r'\.')),
                      "'{0}'".format(new_name), 'g', knowl.id, new_name, referrers] # g means replace all
            self._execute(updator, values)
            if knowl.id in self.cached_titles:
                self.cached_titles[new_name] = self.cached_titles.pop(knowl.id)
            knowl.id = new_name

    def rename_hyphens(self, execute=False):
        selecter = SQL("SELECT DISTINCT ON (id) id FROM kwl_knowls WHERE id LIKE %s")
        bad_names = [rec[0] for rec in db._safe_execute(selecter, ['%-%'])]
        if execute:
            for kid in bad_names:
                new_kid = kid.replace('-', '_')
                print("Renaming %s -> %s" % (kid, new_kid))
                self.rename(kid, new_kid)
        else:
            print(bad_names)

    def broken_links_knowls(self):
        """
        A list of knowl ids that have broken links.

        OUTPUT:

        A list of pairs ``kid``, ``links``, where ``links`` is a list of broken links on the knowl with id ``kid``.
        """
        selecter = SQL("SELECT id, link FROM (SELECT DISTINCT ON (id) id, UNNEST(links) AS link FROM kwl_knowls WHERE status >= 0 ORDER BY id, timestamp DESC) knowls WHERE (SELECT COUNT(*) FROM kwl_knowls kw WHERE kw.id = link) = 0")
        results = defaultdict(list)
        for kid, link in self._safe_execute(selecter):
            results[kid].append(link)
        return [(kid, results[kid]) for kid in sorted(results)]

    def broken_links_code(self):
        """
        A list of code locations that have broken links.

        OUTPUT:

        A list of pairs ``D``, ``links``, where ``D`` is a dictionary with keys
        as in ``code_references``, and ``links`` is a list of purported knowl
        ids that show up in an expression of the form ``KNOWL('BAD_ID')``.
        """
        all_kids = {k['id'] for k in self.get_all_knowls(['id'])}
        matches = subprocess.check_output(['git', 'grep', '-E', '--full-name', '--line-number', '--context', '2', link_finder_re.pattern],encoding='utf-8').split('\n--\n')
        results = []
        for match in matches:
            lines = match.split('\n')
            bad_kids = []
            bad_lines = []
            for line in lines:
                m = grep_extractor.match(line)
                if m and m.group(2) == ':': # active match rather than context
                    bad_kids.extend([kid for kid in extract_links(line) if kid not in all_kids])
                    bad_lines.append(int(m.group(3)))
            if bad_kids:
                # make unique in order preserving way
                seen = set()
                bad_kids = [kid for kid in bad_kids if not (kid in seen or seen.add(kid))]
                processed = self._process_git_grep(match)
                # override the default line numbers, which just test which lines matched the grep
                processed['lines'] = sorted(bad_lines)
                results.append((processed, bad_kids))
        return results

    def is_locked(self, knowlid, delta_min=10):
        """
        if there has been a lock in the last @delta_min minutes, returns a dictionary with the name of the user who obtained a lock and the time it was obtained; else None.
        attention, it discards all locks prior to @delta_min!
        """
        now = utc_now_naive()
        tdelta = timedelta(minutes=delta_min)
        time = now - tdelta
        selecter = SQL("SELECT username, timestamp FROM kwl_locks WHERE id = %s AND timestamp >= %s LIMIT 1")
        L = self._safe_execute(selecter, (knowlid, time))
        if L:
            return dict(zip(["username", "timestamp"], L[0]))

    def set_locked(self, knowl, username):
        """
        when a knowl is edited, a lock is created. username is the user id.
        """
        inserter = SQL("INSERT INTO kwl_locks (id, timestamp, username) VALUES (%s, %s, %s)")
        now = utc_now_naive()
        self._execute(inserter, [knowl.id, now, username])

    def knowl_title(self, kid):
        """
        just the title, used in the knowls in the templates for the pages.
        returns None, if knowl does not exist.
        """
        return self.titles.get(kid)

    def knowl_exists(self, kid, allow_deleted=False):
        """
        checks if the given knowl with ID=@kid exists
        """
        kt = self.knowl_title(kid)
        if kt is not None:
            return True
        k = self.get_knowl(kid, ['id'], beta=True, allow_deleted=allow_deleted)
        return k is not None

    def get_categories(self):
        """
        Returns a dictionary giving the count of (not deleted) knowls within each category.
        """
        selecter = SQL("SELECT cat, COUNT(*) FROM (SELECT DISTINCT ON (id) cat FROM kwl_knowls WHERE type = %s AND status >= 0) knowls GROUP BY cat")
        L = self._safe_execute(selecter, [0])
        return {res[0]: res[1] for res in L}

    def remove_author(self, kid, uid):
        """
        Remove an author from all versions of a knowl.
        """
        updater = SQL("UPDATE kwl_knowls SET authors = array_remove(authors, %s) WHERE id = %s")
        self._execute(updater, [uid, kid])

knowldb = KnowlBackend()

def knowl_title(kid):
    return knowldb.knowl_title(kid)

def knowl_exists(kid):
    return knowldb.knowl_exists(kid)

@cached_function
def knowl_url_prefix():
    """
    if one is running lmfdb in cocalc, front-end javascript (see: lmfdb.js) doesn't know your prefix isn't just a website domain.
    """
    return Configuration().get_url_prefix()

# allowed qualities for knowls
knowl_status_code = {'reviewed':1, 'beta':0, 'in progress': -1, 'deleted': -2}
reverse_status_code = {v:k for k,v in knowl_status_code.items()}
knowl_type_code = {'normal': 0, 'top': 1, 'bottom': -1, 'column': 2}

class Knowl():
    """
    INPUT:

    - ``ID`` -- the knowl id
    - ``template_kwargs`` - the list of additional parameters that
        are passed into the knowl when the knowl is included in the template.
    - ``data`` -- (optional) the dictionary from knowldb with the data for this knowl
    - ``editing`` -- whether this knowl is being displayed in the edit template
        (controls whether all_defines and edit_history are computed)
    - ``showing`` -- whether this knowl is being displayed in the show template
        (controls whether referrers and edit_history are computed)
    - ``allow_deleted`` -- whether the knowl database should return data from deleted knowls with this ID.
    - ``timestamp`` -- desired version of knowl at the given timestamp
    """
    def __init__(self, ID, template_kwargs=None, data=None, editing=False, showing=False,
                 saving=False, renaming=False, allow_deleted=False, timestamp=None):
        self.template_kwargs = template_kwargs or {}

        self.id = ID
        #given that we cache it's existence it is quicker to check for existence
        if data is None:
            if self.exists(allow_deleted=allow_deleted):
                if editing:
                    # as we want to make edits on the most recent version
                    timestamp = None
                data = knowldb.get_knowl(ID,
                        allow_deleted=allow_deleted, timestamp=timestamp)
            else:
                data = {}
        self.title = data.get('title', '')
        self.content = data.get('content', '')
        self.status = data.get('status', 0)
        self.quality = reverse_status_code.get(self.status)
        self.authors = data.get('authors', [])
        # Because category is different from cat, the category will be recomputed when copying knowls.
        self.category = data.get('cat', extract_cat(ID))
        self._last_author = data.get('last_author', data.get('_last_author', ''))
        self.timestamp = ensure_naive_utc(data.get('timestamp', utc_now_naive()))
        self.ms_timestamp = datetime_to_timestamp_in_ms(self.timestamp)
        self.links = data.get('links', [])
        self.defines = data.get('defines', [])
        self.source = data.get('source')
        self.source_name = data.get('source_name')
        self.type = data.get('type')
        self.editing = editing
        # We need to have the source available on comments being created
        if self.type is None:
            self.type, self.source, self.source_name = extract_typ(ID)
        if self.type == 2:
            pieces = ID.split(".")
            # Ignore the title passed in
            if len(pieces) == 3:
                # Column
                self.title = f"Column {pieces[2]} of table {pieces[1]}"
                if pieces[1] in db.tablenames:
                    self.coltype = db[pieces[1]].col_type.get(pieces[2], "DEFUNCT")
                else:
                    self.coltype = "DEFUNCT"
            elif len(pieces) == 2:
                # Table
                self.title = f"Table {pieces[1]}"
                self.coltype = None
                if pieces[1] not in db.tablenames:
                    self.title += " (DEFUNCT)"

        if showing:
            self.comments = knowldb.get_comments(ID)
            if self.type == 0:
                self.referrers = knowldb.ids_referencing(ID)
                self.code_referrers = [code_snippet_knowl(D) for D in knowldb.code_references(self)]
        if saving:
            self.sed_safety = knowldb.check_sed_safety(ID)
        self.reviewed_content = self.reviewed_title = self.reviewed_timestamp = None
        if renaming:
            # This should only occur on beta, so we get the most recent reviewed version
            reviewed_data = knowldb.get_knowl(ID, ['content', 'title', 'timestamp', 'status'], beta=False)
            if reviewed_data and reviewed_data['status'] == 1:
                self.reviewed_content = reviewed_data['content']
                self.reviewed_title = reviewed_data['title']
                self.reviewed_timestamp = ensure_naive_utc(reviewed_data['timestamp'])
        if editing:
            self.all_defines = {k:v for k,v in knowldb.all_defines.items() if len(k) > 3 and k not in common_words and ID not in v}

        if showing or editing:
            self.edit_history = knowldb.get_edit_history(ID)
            # Use to determine whether this is the most recent version of this knowl.
            self.most_recent = not self.edit_history or self.edit_history[-1]['timestamp'] == self.timestamp
            #if not self.edit_history:
            #    # New knowl.  This block should be edited according to the desired behavior for diffs
            #    self.edit_history = [{"timestamp":datetime.now(UTC),
            #                          "last_author":"__nobody__",
            #                          "content":"",
            #                          "status":0}]
            uids = [ elt['last_author'] for elt in self.edit_history]
            if uids:
                full_names = {elt['username']: elt['full_name'] for elt in userdb.full_names(uids)}
            else:
                full_names = {}
            self.previous_review_spot = None
            for i, elt in enumerate(self.edit_history):
                elt['ms_timestamp'] = datetime_to_timestamp_in_ms(elt['timestamp'])
                elt['author_full_name'] = full_names.get(elt['last_author'], "")
                if elt['status'] == 1 and i != len(self.edit_history) - 1:
                    self.previous_review_spot = elt['ms_timestamp']

    def save(self, who, most_recent=None, minor=False):
        """
        INPUT:

        - ``who`` -- the username of the logged in user saving this knowl
        - ``most_recent`` -- if provided, a previous knowl containing authors.
            Currently only used when renaming a knowl.
        - ``minor`` -- if True, don't add the current user to the list of authors.
        """
        if most_recent is not None:
            most_recent = {'authors':most_recent.authors}
        knowldb.save(self, who, most_recent=most_recent, minor=minor)

    def delete(self):
        """Marks the knowl as deleted.  Admin only."""
        knowldb.delete(self)

    def resurrect(self):
        """Brings the knowl back from being deleted by setting status to beta."""
        knowldb.resurrect(self)

    def review(self, who, set_beta=False):
        """Mark the knowl as positively reviewed."""
        knowldb.review(self, who, set_beta=set_beta)

    def start_rename(self, new_name, who):
        knowldb.start_rename(self, new_name, who)

    def undo_rename(self):
        """
        This should be the knowl with the old name, not the new one.
        """
        knowldb.undo_rename(self)

    def actually_rename(self, new_name=None):
        knowldb.actually_rename(self, new_name)

    def author_links(self):
        """
        Basically finds all full names for all the referenced authors.
        (lookup for all full names in just *one* query)
        """
        if not self.authors:
            return []
        return userdb.full_names(self.authors)

    def last_author(self):
        """
        Full names for the last authors.
        (lookup for all full names in just *one* query, hence the or)
        """
        if not self._last_author:
            return ""
        return userdb.lookup(self._last_author)["full_name"]

    def exists(self, allow_deleted=False):
        return knowldb.knowl_exists(self.id, allow_deleted=allow_deleted)

    def data(self, fields=None):
        """
        returns the full database entry or if
        keyword 'fields' is a list of strings,only
        the given fields.
        """
        if not self.title or not self.content:
            data = knowldb.get_knowl(self.id, fields)
            if data:
                self.title = data['title']
                self.content = data['content']
                return data

        data = {'title': self.title,
                'content': self.content}
        return data

    def __unicode__(self):
        return "title: %s, content: %s" % (self.title, self.content)

    def copy(self, **kwds):
        """
        Copy this knowl, with changes described by keyword arguments.

        You can specify a new ID in the keyword arguments, or any of the standard arguments
        available in the data dictionary passed in to the knowl constructor, or any of the
        keyword arguments for the knowl constructor.

        Note that the resulting knowl will not necessarily have all of the same attributes
        set as this one (for example, if you created the knowl with editing=True but
        did not pass editing=True into this method, the result will not have ``all_defines`` set.

        Because of the way the Knowl constructor works, the category will be recomputed from the ID.
        """
        ID = kwds.pop('ID', self.id)
        if 'data' in kwds:
            data = kwds.pop('data')
        else:
            data = dict(self.__dict__)
            for key in data:
                if key in kwds:
                    data[key] = kwds.pop(key)
        return Knowl(ID, data=data, **kwds)
