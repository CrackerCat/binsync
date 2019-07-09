
import time
import threading
import os

import git

from .data import User
from .state import State
from .errors import MetadataNotFoundError


class Client:
    """
    The binsync Client.

    :ivar str master_user:  User name of the master user.
    :ivar str repo_root:    Local path of the Git repo.
    :ivar str remote:       Git remote.
    :ivar str branch:       Git branch.
    :ivar int _commit_interval: The interval for committing local changes into the Git repo, pushing to the remote
                            side, and pulling from the remote.
    """
    def __init__(self, master_user, repo_root, remote="origin", branch="master", commit_interval=10, init_repo=False,
                 remote_url=None):
        self.master_user = master_user
        self.repo_root = repo_root
        self.remote = remote
        self.branch = branch

        # three scenarios
        # 1. We already have the repo checked out
        # 2. We haven't checked out the repo, but there is a remote repo. In this case, we clone the repo from
        #    @remote_url
        # 3. There is no such repo, and we are the very first group of people trying to setup this repo. In this case,
        #    @init_repo should be True, and we will initialize the repo.

        try:
            # case 1
            # open the local repo
            self.repo = git.Repo(self.repo_root)
        except (git.NoSuchPathError, git.InvalidGitRepositoryError):
            # initialization
            assert not (init_repo is True and remote_url)
            if init_repo:
                # case 3
                git.Repo.init(self.repo_root)
            elif remote_url is not None:
                # case 2
                self.clone(remote_url)
            self.repo = git.Repo(self.repo_root)

        assert not self.repo.bare  # it should not be a bare repo

        self._commit_interval = commit_interval
        self._worker_thread = None

        # timestamps
        self._last_commit_ts = 0

        self.state = None  # TODO: Updating it
        self.commit_lock = threading.Lock()

    @property
    def has_remote(self):
        """
        If there is a remote configured for our local repo.

        :return:    True if there is a remote, False otherwise.
        """
        return self.remote in self.repo.remotes

    @property
    def last_update_timestamp(self):
        return self._last_commit_ts

    def add_remote(self, name, remote_url):
        """
        Add a remote to the local repo.

        :param name:
        :param remote_url:
        :return:
        """

        self.repo.create_remote(name, url=remote_url)

    def clone(self, remote_url):
        """
        Checkout from a remote_url to a local path specified by self.local_root.

        :param str remote_url:  The URL of the Git remote.
        :return:                None
        """

        git.Repo.clone_from(remote_url, self.repo_root)

    def pull(self):
        """
        Pull changes from the remote side.

        :return:    None
        """

        self.repo.remotes[self.remote].pull()

    def push(self):
        """
        Push local changes to the remote side.

        :return:    None
        """

        self.repo.remotes[self.remote].push()

    def users(self):
        for d in os.listdir(self.repo_root):
            metadata_path = os.path.join(self.repo_root, d, "metadata.toml")
            if os.path.isfile(metadata_path):
                # Load metadata
                metadata = State.load_metadata(metadata_path)
                yield User.from_metadata(metadata)

    def base_path(self, user=None):
        if user is None:
            user = self.master_user
        return os.path.join(self.repo_root, user)

    def get_state(self, user=None, version=None):
        if user is None or user == self.master_user:
            # local state
            if self.state is None:
                try:
                    self.state = State.parse(self.base_path(user=user), version=version)
                except MetadataNotFoundError:
                    # we should return a new state
                    self.state = State(user if user is not None else self.master_user)
            return self.state
        else:
            try:
                state = State.parse(self.base_path(user=user), version=version)
                return state
            except MetadataNotFoundError:
                return None

    def get_locked_state(self, user=None, version=None):
        with self.commit_lock:
            yield self.get_state(user=user, version=version)

    def start_auto(self):
        if self._worker_thread is None:
            self._worker_thread = threading.Thread(target=self._worker_routine)
            self._worker_thread.start()
        else:
            raise Exception("start_auto() should not be called twice. There is already a worker thread running.")

    def _worker_routine(self):
        while True:
            time.sleep(0.5)
            ts = time.time()

            if ts - self._last_commit_ts > self._commit_interval:
                self.update()

    def update(self):
        """

        :return:
        """

        # do a pull... if there is a remote
        if self.has_remote:
            self.pull()

        if self.get_state().dirty:
            # do a save!
            self.save_state()

        if self.has_remote:
            # do a push... if there is a remote
            self.push()

        self._last_commit_ts = time.time()

    def save_state(self, state=None):

        if state is None:
            state = self.state

        # you don't want to save as another user... unless you want to mess things up for your collaborators, in which
        # case, please comment out the following assertion.
        assert self.master_user == state.user

        path = self.base_path(user=state.user)

        if not os.path.exists(path):
            # create this folder if it does not exist
            os.mkdir(path)

        # dump the state
        state.dump(path)

        # commit changes
        self.repo.index.add([os.path.join(".", state.user, "*")])
        self.repo.index.commit("Save state")

    def close(self):
        self.repo.close()
        del self.repo
