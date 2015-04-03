""" Code to allow module store to interface with courseware index """
from __future__ import absolute_import

from datetime import timedelta
import logging
import re

from django.conf import settings
from django.utils.translation import ugettext as _

from contentstore.utils import course_image_url
from eventtracking import tracker
from search.search_engine_base import SearchEngine
from xmodule.modulestore import ModuleStoreEnum
from xmodule.modulestore.exceptions import ItemNotFoundError
from xmodule.annotator_mixin import html_to_text

# Use default index and document names for now
INDEX_NAME = "courseware_index"
COURSEWARE_DOCUMENT_TYPE = "courseware_content"
DISCOVERY_DOCUMENT_TYPE = "course_info"

# REINDEX_AGE is the default amount of time that we look back for changes
# that might have happened. If we are provided with a time at which the
# indexing is triggered, then we know it is safe to only index items
# recently changed at that time. This is the time period that represents
# how far back from the trigger point to look back in order to index
REINDEX_AGE = timedelta(0, 60)  # 60 seconds

log = logging.getLogger('edx.modulestore')


def fetch_from_about(modulestore, course_id, attribute):
    """ Get about attribute from modulestore """
    usage_key = course_id.make_usage_key('about', attribute)
    try:
        value = modulestore.get_item(usage_key).data
    except ItemNotFoundError:
        value = None
    return value


def fetch_course_information_detail(modulestore, course, about_information):
    """
    Fetch the course information attribute for the given course's attribute
    from the correct persistence location and return its value.
    """
    if about_information[2]:
        return fetch_from_about(modulestore, course.id, about_information[0])
    else:
        return getattr(course, about_information[0], None)


def strip_html_content_to_text(html_content):
    """ Gets only the textual part for html content - useful for buildling text to be searched """
    # Removing HTML-encoded non-breaking space characters
    text_content = re.sub(r"(\s|&nbsp;|//)+", " ", html_to_text(html_content))
    # Removing HTML CDATA
    text_content = re.sub(r"<!\[CDATA\[.*\]\]>", "", text_content)
    # Removing HTML comments
    text_content = re.sub(r"<!--.*-->", "", text_content)

    return text_content


def indexing_is_enabled():
    """
    Checks to see if the indexing feature is enabled
    """
    return settings.FEATURES.get('ENABLE_COURSEWARE_INDEX', False)


class SearchIndexingError(Exception):
    """ Indicates some error(s) occured during indexing """

    def __init__(self, message, error_list):
        super(SearchIndexingError, self).__init__(message)
        self.error_list = error_list


class CoursewareSearchIndexer(object):
    """
    Class to perform indexing for courseware search from different modulestores
    """

    # Flags for where to index the information
    ANALYSE = 1 << 0  # Add the information to the analysed content of the index
    PROPERTY = 1 << 1  # Add the information as a property of the object being indexed (not analysed)

    # List of properties to add to the index - each item in the list is a tuple of:
    #   1) Property name to use
    #   2) Where to add in the index (using flags above)
    #   3) Whether the property is found in the 'about' area of the modulestore
    #      (True) or is a property directly on the course object
    ABOUT_INFORMATION_TO_INCLUDE = [
        ("advertised_start", PROPERTY, False),
        ("announcement", PROPERTY, True),
        ("start", PROPERTY, False),
        ("end", PROPERTY, False),
        ("effort", PROPERTY, True),
        ("display_name", ANALYSE, False),
        ("overview", ANALYSE, True),
        ("title", ANALYSE | PROPERTY, True),
        ("university", ANALYSE | PROPERTY, True),
        ("number", ANALYSE | PROPERTY, False),
        ("short_description", ANALYSE, True),
        ("description", ANALYSE, True),
        ("key_dates", ANALYSE, True),
        ("video", ANALYSE, True),
        ("course_staff_short", ANALYSE, True),
        ("course_staff_extended", ANALYSE, True),
        ("requirements", ANALYSE, True),
        ("syllabus", ANALYSE, True),
        ("textbook", ANALYSE, True),
        ("faq", ANALYSE, True),
        ("more_info", ANALYSE, True),
        ("ocw_links", ANALYSE, True),
        ("enrollment_start", PROPERTY, False),
        ("enrollment_end", PROPERTY, False),
    ]

    @classmethod
    def index_about_information(cls, modulestore, course):
        """
        Add the given course to the course discovery index

        Arguments:
        modulestore - modulestore object to use for operations

        course - course object from which to take properties, locate about information
        """
        searcher = SearchEngine.get_search_engine(INDEX_NAME)
        if 'DISABLE_START_DATES' not in settings.FEATURES:
            settings.FEATURES['DISABLE_START_DATES'] = False
        if not searcher:
            return

        course_id = unicode(course.id)
        try:
            course_info = {
                'id': course_id,
                'course': course_id,
                'content': {},
                'image_url': course_image_url(course),
            }

            for about_information in cls.ABOUT_INFORMATION_TO_INCLUDE:
                section_content = fetch_course_information_detail(modulestore, course, about_information)
                if section_content:
                    if isinstance(section_content, basestring):
                        section_content = strip_html_content_to_text(section_content)
                    if about_information[1] & cls.ANALYSE:
                        course_info['content'][about_information[0]] = section_content
                    if about_information[1] & cls.PROPERTY:
                        course_info[about_information[0]] = section_content

            searcher.index(DISCOVERY_DOCUMENT_TYPE, course_info)
        except Exception as err:
            log.exception(
                "Course discovery indexing error encountered, course discovery index may be out of date %s - %r",
                course_id,
                err
            )
            raise
        else:
            log.debug(
                "Successfully added %s course to the course discovery index",
                course_id
            )

    @classmethod
    def index_course(cls, modulestore, course_key, triggered_at=None, reindex_age=REINDEX_AGE):
        """
        Process course for indexing

        Arguments:
        modulestore - modulestore object to use for operations

        course_key (CourseKey) - course identifier

        triggered_at (datetime) - provides time at which indexing was triggered;
            useful for index updates - only things changed recently from that date
            (within REINDEX_AGE above ^^) will have their index updated, others skip
            updating their index but are still walked through in order to identify
            which items may need to be removed from the index
            If None, then a full reindex takes place

        Returns:
        Number of items that have been added to the index
        """
        error_list = []
        searcher = SearchEngine.get_search_engine(INDEX_NAME)
        if not searcher:
            return

        location_info = {
            "course": unicode(course_key),
        }

        # Wrap counter in dictionary - otherwise we seem to lose scope inside the embedded function `index_item`
        indexed_count = {
            "count": 0
        }

        # indexed_items is a list of all the items that we wish to remain in the
        # index, whether or not we are planning to actually update their index.
        # This is used in order to build a query to remove those items not in this
        # list - those are ready to be destroyed
        indexed_items = set()

        def index_item(item, skip_index=False):
            """
            Add this item to the search index and indexed_items list

            Arguments:
            item - item to add to index, its children will be processed recursively

            skip_index - simply walk the children in the tree, the content change is
                older than the REINDEX_AGE window and would have been already indexed.
                This should really only be passed from the recursive child calls when
                this method has determined that it is safe to do so
            """
            is_indexable = hasattr(item, "index_dictionary")
            item_index_dictionary = item.index_dictionary() if is_indexable else None
            # if it's not indexable and it does not have children, then ignore
            if not item_index_dictionary and not item.has_children:
                return

            item_id = unicode(item.scope_ids.usage_id)
            indexed_items.add(item_id)
            if item.has_children:
                # determine if it's okay to skip adding the children herein based upon how recently any may have changed
                skip_child_index = skip_index or \
                    (triggered_at is not None and (triggered_at - item.subtree_edited_on) > reindex_age)
                for child_item in item.get_children():
                    index_item(child_item, skip_index=skip_child_index)

            if skip_index or not item_index_dictionary:
                return

            item_index = {}
            # if it has something to add to the index, then add it
            try:
                item_index.update(location_info)
                item_index.update(item_index_dictionary)
                item_index['id'] = item_id
                if item.start:
                    item_index['start_date'] = item.start

                searcher.index(COURSEWARE_DOCUMENT_TYPE, item_index)
                indexed_count["count"] += 1
            except Exception as err:  # pylint: disable=broad-except
                # broad exception so that index operation does not fail on one item of many
                log.warning('Could not index item: %s - %r', item.location, err)
                error_list.append(_('Could not index item: {}').format(item.location))

        def remove_deleted_items():
            """
            remove any item that is present in the search index that is not present in updated list of indexed items
            as we find items we can shorten the set of items to keep
            """
            response = searcher.search(
                doc_type=COURSEWARE_DOCUMENT_TYPE,
                field_dictionary={"course": unicode(course_key)},
                exclude_ids=indexed_items
            )
            result_ids = [result["data"]["id"] for result in response["results"]]
            for result_id in result_ids:
                searcher.remove(COURSEWARE_DOCUMENT_TYPE, result_id)

        try:
            with modulestore.branch_setting(ModuleStoreEnum.RevisionOption.published_only):
                course = modulestore.get_course(course_key, depth=None)

                # First add the top-level about information for the course
                cls.index_about_information(modulestore, course)

                # Next index the content
                for item in course.get_children():
                    index_item(item)
                remove_deleted_items()
        except Exception as err:  # pylint: disable=broad-except
            # broad exception so that index operation does not prevent the rest of the application from working
            log.exception(
                "Indexing error encountered, courseware index may be out of date %s - %r",
                course_key,
                err
            )
            error_list.append(_('General indexing error occurred'))

        if error_list:
            raise SearchIndexingError(_('Error(s) present during indexing'), error_list)

        return indexed_count["count"]

    @classmethod
    def do_course_reindex(cls, modulestore, course_key):
        """
        (Re)index all content within the given course, tracking the fact that a full reindex has taking place
        """
        indexed_count = cls.index_course(modulestore, course_key)
        if indexed_count:
            cls._track_index_request('edx.course.index.reindexed', indexed_count)
        return indexed_count

    @classmethod
    def _track_index_request(cls, event_name, indexed_count):
        """Track content index requests.

        Arguments:
            event_name (str):  Name of the event to be logged.
        Returns:
            None

        """
        data = {
            "indexed_count": indexed_count,
            'category': 'courseware_index',
        }

        tracker.emit(
            event_name,
            data
        )
