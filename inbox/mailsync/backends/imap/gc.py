import datetime
import gevent
from inbox.log import get_logger
from inbox.models import Message
from inbox.models.session import session_scope
from inbox.util.concurrency import retry_and_report_killed

log = get_logger()

DEFAULT_MESSAGE_TTL = 120


class DeleteHandler(gevent.Greenlet):
    """We don't outright delete message objects when all their associated
    ImapUids are deleted. Instead, we mark them by setting a deleted_at
    timestamp. This is so that we can identify when a message is moved between
    folders, or when a draft is updated.

    This class is responsible for periodically checking for marked messages,
    and deleting them for good if they've been marked as deleted for longer
    than message_ttl seconds."""
    def __init__(self, account_id, namespace_id,
                 message_ttl=DEFAULT_MESSAGE_TTL):
        self.account_id = account_id
        self.namespace_id = namespace_id
        self.log = log.new(account_id=account_id)
        self.message_ttl = datetime.timedelta(seconds=message_ttl)
        gevent.Greenlet.__init__(self)

    def _run(self):
        return retry_and_report_killed(self._run_impl,
                                       account_id=self.account_id)

    def _run_impl(self):
        while True:
            self.check()
            gevent.sleep(self.message_ttl)

    def check(self):
        current_time = datetime.datetime.utcnow()
        with session_scope() as db_session:
            dangling_messages = db_session.query(Message).filter(
                Message.namespace_id == self.namespace_id,
                Message.deleted_at < current_time - self.message_ttl)
            affected_threads = set()
            for message in dangling_messages:
                # If the message isn't *actually* dangling (i.e., it has
                # imapuids associated with it), undelete it.
                if message.imapuids:
                    message.deleted_at = None
                else:
                    affected_threads.add(message.thread)
                    db_session.delete(message)

            db_session.commit()
            for thread in affected_threads:
                if not thread.messages:
                    db_session.delete(thread)
            db_session.commit()
