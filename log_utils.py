import logging

import github_action_utils as gha_utils


class GHAHandler(logging.Handler):

    def emit(self, record):
        if record.levelno >= logging.ERROR:
            gha_utils.error(self.format(record))
        elif record.levelno >= logging.WARNING:
            gha_utils.warning(self.format(record))
        elif record.levelno >= logging.INFO:
            gha_utils.notice(self.format(record))
        elif record.levelno >= logging.DEBUG:
            gha_utils.debug(self.format(record))
