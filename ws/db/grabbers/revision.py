#!/usr/bin/env python3

import logging

import sqlalchemy as sa

import ws.utils
from ws.utils import value_or_none
from ws.parser_helpers.title import Title
from ws.db.selects import logevents

from . import Grabber

logger = logging.getLogger(__name__)

# TODO: allow syncing content independently from revisions
# TODO: are truncated results due to PHP cache reflected by changing the query-continuation parameter accordingly or do we actually lose some revisions?
class GrabberRevisions(Grabber):

    def __init__(self, api, db, with_content=False):
        super().__init__(api, db)
        self.with_content = with_content

        ins_text = sa.dialects.postgresql.insert(db.text)
        ins_revision = sa.dialects.postgresql.insert(db.revision)
        ins_archive = sa.dialects.postgresql.insert(db.archive)
        ins_tgrev = sa.dialects.postgresql.insert(db.tagged_revision)
        ins_tgar = sa.dialects.postgresql.insert(db.tagged_archived_revision)

        self.sql = {
            ("insert", "text"):
                ins_text.on_conflict_do_update(
                    constraint=db.text.primary_key,
                    set_={
                        "old_text":  ins_text.excluded.old_text,
                        "old_flags": ins_text.excluded.old_flags,
                    }),
            ("insert", "revision"):
                ins_revision.on_conflict_do_update(
                    constraint=db.revision.primary_key,
                    set_={
                        # this should be the only column that may change with an insert query
                        "rev_text_id": ins_revision.excluded.rev_text_id,
                    }),
            ("insert", "archive"):
                ins_archive.on_conflict_do_update(
                    index_elements=[db.archive.c.ar_rev_id],
                    set_={
                        # this should be the only column that may change with an insert query
                        "ar_text_id": ins_archive.excluded.ar_text_id,
                    }),
            ("insert", "tagged_revision"):
                ins_tgrev.values(
                    tgrev_rev_id=sa.bindparam("b_rev_id"),
                    tgrev_tag_id=sa.select([db.tag.c.tag_id]) \
                                        .where(db.tag.c.tag_name == sa.bindparam("b_tag_name"))) \
                    .on_conflict_do_nothing(),
            ("insert", "tagged_archived_revision"):
                ins_tgar.values(
                    tgar_rev_id=sa.bindparam("b_rev_id"),
                    tgar_tag_id=sa.select([db.tag.c.tag_id]) \
                                        .where(db.tag.c.tag_name == sa.bindparam("b_tag_name"))) \
                    .on_conflict_do_nothing(),
            ("delete", "tagged_revision"):
                db.tagged_revision.delete() \
                    .where(sa.and_(db.tagged_revision.c.tgrev_rev_id == sa.bindparam("b_rev_id"),
                                   db.tagged_revision.c.tgrev_tag_id == sa.select([db.tag.c.tag_id]) \
                                            .where(db.tag.c.tag_name == sa.bindparam("b_tag_name")))),
            ("delete", "tagged_archived_revision"):
                db.tagged_archived_revision.delete() \
                    .where(sa.and_(db.tagged_archived_revision.c.tgar_rev_id == sa.bindparam("b_rev_id"),
                                   db.tagged_archived_revision.c.tgar_tag_id == sa.select([db.tag.c.tag_id]) \
                                            .where(db.tag.c.tag_name == sa.bindparam("b_tag_name")))),
            # query for updating archive.ar_page_id
            ("update", "archive.ar_page_id"):
                db.archive.update() \
                    .where(sa.and_(db.archive.c.ar_namespace == sa.bindparam("b_namespace"),
                                   db.archive.c.ar_title == sa.bindparam("b_title"))),
            ("merge", "revision"):
                db.revision.update() \
                    .where(sa.and_(db.revision.c.rev_page == sa.bindparam("b_src_page_id"),
                                   # MW defect: timestamp-based merge points are not sufficient,
                                   # see https://phabricator.wikimedia.org/T183501
                                   db.revision.c.rev_timestamp <= sa.bindparam("b_mergepoint")))
                    .values(rev_page=sa.select([db.page.c.page_id])
                                .where(sa.and_(db.page.c.page_namespace == sa.bindparam("b_dest_ns"),
                                               db.page.c.page_title == sa.bindparam("b_dest_title"))
                                )
                    ),
            ("update", "rev_deleted"):
                db.revision.update() \
                    .where(db.revision.c.rev_id == sa.bindparam("b_revid")),
            ("update", "ar_deleted"):
                db.archive.update() \
                    .where(db.archive.c.ar_rev_id == sa.bindparam("b_revid")),
        }

        # build query to move data from the archive table into revision
        deleted_revision = db.archive.delete() \
            .where(db.archive.c.ar_page_id == sa.bindparam("b_page_id")) \
            .returning(*db.archive.c._all_columns) \
            .cte("deleted_revision")
        columns = [
                deleted_revision.c.ar_rev_id,
                deleted_revision.c.ar_page_id,
                deleted_revision.c.ar_text_id,
                deleted_revision.c.ar_comment,
                deleted_revision.c.ar_user,
                deleted_revision.c.ar_user_text,
                deleted_revision.c.ar_timestamp,
                deleted_revision.c.ar_minor_edit,
                deleted_revision.c.ar_deleted,
                deleted_revision.c.ar_len,
                deleted_revision.c.ar_parent_id,
                deleted_revision.c.ar_sha1,
                deleted_revision.c.ar_content_model,
                deleted_revision.c.ar_content_format,
            ]
        insert = db.revision.insert().from_select(
            db.revision.c._all_columns,
            sa.select(columns).select_from(deleted_revision)
        )
        self.sql["move", "revision"] = insert

        # build query to move data from the tagged_archived_revision table into tagged_revision
        deleted_tagged_archived_revision = db.tagged_archived_revision.delete() \
            .where(db.tagged_archived_revision.c.tgar_rev_id.in_(
                        sa.select([db.archive.c.ar_rev_id]) \
                            .select_from(db.archive) \
                            .where(db.archive.c.ar_page_id == sa.bindparam("b_page_id"))
                        )
                    ) \
            .returning(*db.tagged_archived_revision.c._all_columns) \
            .cte("deleted_tagged_archived_revision")
        insert = db.tagged_revision.insert().from_select(
            db.tagged_revision.c._all_columns,
            deleted_tagged_archived_revision.select()
        )
        self.sql["move", "tagged_archived_revision"] = insert

        props = "ids|timestamp|flags|user|userid|comment|size|sha1|contentmodel|tags"
        if self.with_content is True:
            props += "|content"

        self.arv_params = {
            "list": "allrevisions",
            "arvprop": props,
            "arvlimit": "max",
        }

        self.adr_params = {
            "list": "alldeletedrevisions",
            "adrprop": props,
            "adrlimit": "max",
        }

        # TODO: check the permission to view deleted revisions
#        if "patrol" in self.api.user.rights:
#            self.rc_params["rcprop"] += "|patrolled"
#        else:
#            logger.warning("You need the 'patrol' right to request the patrolled flag. "
#                           "Skipping it, but the sync will be incomplete.")

    # TODO: text.old_id is auto-increment, but revision.rev_text_id has to be set accordingly. SQL should be able to do it automatically.
    def _get_text_id_gen(self):
        conn = self.db.engine.connect()
        result = conn.execute(sa.select( [sa.sql.func.max(self.db.text.c.old_id)] ))
        value = result.fetchone()[0]
        if value is None:
            value = 0
        while True:
            value += 1
            yield value

    def gen_text(self, rev, text_id):
        db_entry = {
            "old_id": text_id,
            "old_text": rev["*"],
            "old_flags": "utf-8",
        }
        yield self.sql["insert", "text"], db_entry

    def gen_revisions(self, page):
        for rev in page["revisions"]:
            db_entry = {
                "rev_id": rev["revid"],
                "rev_page": value_or_none(page.get("pageid")),
                "rev_comment": rev["comment"],
                "rev_user": rev["userid"],
                "rev_user_text": rev["user"],
                "rev_timestamp": rev["timestamp"],
                "rev_minor_edit": "minor" in rev,
                # rev_deleted is set separately with an update query, see below
                "rev_len": rev["size"],
                "rev_parent_id": rev.get("parentid"),
                "rev_sha1": rev["sha1"],
                "rev_content_model": rev["contentmodel"],        # always available
                "rev_content_format": rev.get("contentformat"),  # available iff content is available
            }

            if self.with_content is True:
                text_id = next(self.text_id_gen)
                db_entry["rev_text_id"] = text_id
                yield from self.gen_text(rev, text_id)

            yield self.sql["insert", "revision"], db_entry

            for tag_name in rev.get("tags", []):
                db_entry = {
                    "b_rev_id": rev["revid"],
                    "b_tag_name": tag_name,
                }
                yield self.sql["insert", "tagged_revision"], db_entry

    def gen_deletedrevisions(self, page):
        title = Title(self.api, page["title"])
        for rev in page["revisions"]:
            db_entry = {
                "ar_namespace": page["ns"],
                "ar_title": title.dbtitle(page["ns"]),
                "ar_rev_id": rev["revid"],
                # ar_page_id is not visible through the API: https://phabricator.wikimedia.org/T183398
                # (page["pageid"] is either 0 or the ID of a new page which was created without undeleting these revisions)
                "ar_page_id": None,
                "ar_comment": rev["comment"],
                "ar_user": rev["userid"],
                "ar_user_text": rev["user"],
                "ar_timestamp": rev["timestamp"],
                "ar_minor_edit": "minor" in rev,
                # ar_deleted is set separately with an update query, see below
                "ar_len": rev["size"],
                # ar_parent_id is not visible through API: https://phabricator.wikimedia.org/T183376
                "ar_sha1": rev["sha1"],
                "ar_content_model": rev["contentmodel"],        # always available
                "ar_content_format": rev.get("contentformat"),  # available iff content is available
            }

            if self.with_content is True:
                text_id = next(self.text_id_gen)
                db_entry["ar_text_id"] = text_id
                yield from self.gen_text(rev, text_id)

            yield self.sql["insert", "archive"], db_entry

            for tag_name in rev.get("tags", []):
                db_entry = {
                    "b_rev_id": rev["revid"],
                    "b_tag_name": tag_name,
                }
                yield self.sql["insert", "tagged_archived_revision"], db_entry

    # TODO: write custom insert and update methods, use discontinued API queries and wrap each chunk in a separate transaction
    # TODO: generalize the above even for logging table

    def gen_insert(self):
        # we need one instance per transaction
        self.text_id_gen = self._get_text_id_gen()

        for page in self.api.list(self.arv_params):
            yield from self.gen_revisions(page)
        for page in self.api.list(self.adr_params):
            yield from self.gen_deletedrevisions(page)

    def gen_update(self, since):
        # we need one instance per transaction
        self.text_id_gen = self._get_text_id_gen()

        # TODO: make sure that the updates from the API don't create a duplicate row with a new ID in the text table

        # save new revids for the tag updates
        new_revids = set()
        new_deleted_revids = set()

        arv_params = self.arv_params.copy()
        arv_params["arvdir"] = "newer"
        arv_params["arvstart"] = since
        for page in self.api.list(arv_params):
            yield from self.gen_revisions(page)
            for rev in page["revisions"]:
                new_revids.add(rev["revid"])

        deleted_pages = set()
        undeleted_pages = {}
        merged_pages = {}
        moved_pages = set()
        deleted_revisions = {}
        added_tags = {}
        removed_tags = {}
        imported_pages = set()

        le_params = {
            "prop": {"type", "details", "title", "ids"},
            "dir": "newer",
            "start": since,
        }
        for le in logevents.list(self.db, le_params):
            # check logevents for delete/undelete
            if le["type"] == "delete":
                if le["action"] == "delete":
                    deleted_pages.add(le["title"])
                    # keep only the most recent action
                    if le["title"] in undeleted_pages:
                        del undeleted_pages[le["title"]]
                elif le["action"] == "restore":
                    undeleted_pages[le["title"]] = le["logpage"]
                    # keep only the most recent action
                    if le["title"] in deleted_pages:
                        deleted_pages.remove(le["title"])
                elif le["action"] == "revision":
                    assert le["params"]["type"] == "revision"
                    for revid in le["params"]["ids"]:
                        deleted_revisions[revid] = le["params"]["new"]["bitmask"]
            # check imported pages
            elif le["type"] == "import":
                imported_pages.add(le["logpage"])
            # check logevents for merge
            elif le["type"] == "merge":
                merged_pages[le["logpage"]] = le["params"]
            # we need also moved pages for a safeguard due to a MW defect (see below)
            elif le["type"] == "move":
                moved_pages.add(le["logpage"])
            # check added/removed tags
            elif le["type"] == "tag" and le["action"] == "update":
                # skip tags for logevents
                if "revid" in le["params"]:
                    _revid = le["params"]["revid"]
                    # skip new revids - tags for those are added in self.gen_revisions and self.gen_deletedrevisions
                    if _revid not in new_revids and _revid not in new_deleted_revids:
                        _added = set(le["params"]["tagsAdded"])
                        _removed = set(le["params"]["tagsRemoved"])
                        assert _added & _removed == set()
                        for _tag in _added:
                            if _tag in removed_tags.get(_revid, set()):
                                removed_tags[_revid].remove(_tag)
                            else:
                                added_tags.setdefault(_revid, set())
                                added_tags[_revid].add(_tag)
                        for _tag in _removed:
                            if _tag in added_tags.get(_revid, set()):
                                added_tags[_revid].remove(_tag)
                            else:
                                removed_tags.setdefault(_revid, set())
                                removed_tags[_revid].add(_tag)

        # handle undelete - move the rows from archive to revision (deletes are handled in the page grabber)
        for _title, pageid in undeleted_pages.items():
            # ar_page_id is apparently not visible via list=alldeletedrevisions,
            # so we have to update it here first
            title = Title(self.api, _title)
            ns = title.namespacenumber
            dbtitle = title.dbtitle(ns),
            yield self.sql["update", "archive.ar_page_id"], {"b_namespace": ns, "b_title": dbtitle, "ar_page_id": pageid}
            # move tags first
            yield self.sql["move", "tagged_archived_revision"], {"b_page_id": pageid}
            # move the updated rows from archive to revision
            yield self.sql["move", "revision"], {"b_page_id": pageid}

        # MW defect: it is not possible to use list=alldeletedrevisions to get all new
        # deleted revisions the same way as normal revisions, because adrstart can be
        # used only along with adruser (archive.ar_timestamp is not indexed separately).
        #
        # To work around this, we realize that new deleted revisions can appear only by
        # deleting an existing page, which creates an entry in the logging table. We
        # still need to query the API with prop=deletedrevisions to get even the
        # revisions that were created and deleted since the last sync.
        for chunk in ws.utils.iter_chunks(deleted_pages, self.api.max_ids_per_query):
            params = {
                "action": "query",
                "titles": "|".join(chunk),
                "prop": "deletedrevisions",
                "drvprop": self.adr_params["adrprop"],
                "drvlimit": "max",
                "drvstart": since,
                "drvdir": "newer",
            }
            result = self.api.call_api(params, expand_result=False)
            # TODO: handle 'drvcontinue'
            if "drvcontinue" in result:
                raise NotImplementedError("Handling of the 'drvcontinue' parameter is not implemented.")
            for page in result["query"]["pages"].values():
                if "deletedrevisions" in page:
                    # update the dict for gen_deletedrevisions to understand
                    page["revisions"] = page.pop("deletedrevisions")
                    yield from self.gen_deletedrevisions(page)
                    for rev in page["revisions"]:
                        new_revids.add(rev["revid"])

        # sync all revisions of imported pages
        for chunk in ws.utils.iter_chunks(imported_pages, self.api.max_ids_per_query):
            params = {
                "action": "query",
                "pageids": "|".join(str(i) for i in chunk),
                "prop": "revisions|deletedrevisions",
                "rvprop": self.arv_params["arvprop"],
                "drvprop": self.adr_params["adrprop"],
                "rvlimit": "max",
                "drvlimit": "max",
                "rvdir": "newer",
                "drvdir": "newer",
            }
            result = self.api.call_api(params, expand_result=False)
            # TODO: handle 'rvcontinue' and 'drvcontinue'
            if "rvcontinue" in result or "drvcontinue" in result:
                raise NotImplementedError("Handling of the 'rvcontinue' and 'drvcontinue' parameters is not implemented.")
            for page in result["query"]["pages"].values():
                if "revisions" in page:
                    yield from self.gen_revisions(page)
                    for rev in page["revisions"]:
                        new_revids.add(rev["revid"])
                if "deletedrevisions" in page:
                    # update the dict for gen_deletedrevisions to understand
                    page["revisions"] = page.pop("deletedrevisions")
                    yield from self.gen_deletedrevisions(page)
                    for rev in page["revisions"]:
                        new_revids.add(rev["revid"])

        # handle merge
        # MW defect: the target page ID is not present in the logevent, so we need to look up
        # by namespace and title - see https://phabricator.wikimedia.org/T183504
        # Hence, we abort if we see that the target page has been moved - in that case we
        # cannot safely determine the target page. Let's hope it never happens in practice,
        # sync as often as possible to avoid this.
        for pageid, params in merged_pages.items():
            if pageid in moved_pages:
                raise NotImplementedError("Cannot merge revisions from [[{}]] to [[{}]]: target page has been moved.")
            yield self.sql["merge", "revision"], {"b_src_page_id": pageid,
                                                  "b_dest_ns": params["dest_ns"],
                                                  "b_dest_title": params["dest_title"],
                                                  "b_mergepoint": params["mergepoint"]}

        # update rev_deleted and ar_deleted
        # Note that the log events do not tell if it applies to normal or archived revision,
        # so we need to issue queries against both tables, even though each time only one
        # will actually do something.
        for revid, bitmask in deleted_revisions.items():
            yield self.sql["update", "rev_deleted"], {"b_revid": revid, "rev_deleted": bitmask}
            yield self.sql["update", "ar_deleted"], {"b_revid": revid, "ar_deleted": bitmask}

        # update tags
        for revid, added in added_tags.items():
            for tag in added:
                # Note that the log events do not tell if it applies to normal or archived
                # revision. For inserts we have to check it manually, otherwise we would
                # get foreign key errors. Revisions added in this sync are skipped, so we
                # don't mind if the queued queries were not executed yet.
                db_entry = {
                    "b_rev_id": revid,
                    "b_tag_name": tag,
                }
                result = self.db.engine.execute(sa.select([
                            sa.exists().where(self.db.revision.c.rev_id == revid)
                        ]))
                if result.fetchone()[0]:
                    yield self.sql["insert", "tagged_revision"], db_entry
                else:
                    yield self.sql["insert", "tagged_archived_revision"], db_entry

        for revid, removed in removed_tags.items():
            for tag in removed:
                # Note that the log events do not tell if it applies to normal or archived
                # revision, so we issue queries against both tables, even though each time
                # only one will actually do something.
                db_entry = {
                    "b_rev_id": revid,
                    "b_tag_name": tag,
                }
                yield self.sql["delete", "tagged_revision"], db_entry
                yield self.sql["delete", "tagged_archived_revision"], db_entry
