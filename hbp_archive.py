# Copyright (c) 2017-2018 CNRS
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
A high-level API for interacting with the Human Brain Project archival storage at CSCS.

Author: Andrew Davison and Shailesh Appukuttan, CNRS

Usage:

    from hbp_archive import Container, PublicContainer, Project, Archive

    # Working with a public container

    container = PublicContainer("https://object.cscs.ch/v1/AUTH_id/my_container")
    files = container.list()
    local_file = container.download("README.txt")
    print(container.read("README.txt"))
    number_of_files = container.count()
    size_in_MB = container.size("MB")

    # Working with a private container

    container = Container("MyContainer", username="xyzabc")  # you will be prompted for your password
    files = container.list()
    local_file = container.download("README.txt", overwrite=True)  # default is not to overwrite existing files
    print(container.read("README.txt"))
    number_of_files = container.count()
    size_in_MB = container.size("MB")

    container.move("my_file.dat", "a_subdirectory", "new_name.dat")  # move/rename file within a container

    # Reading a file directly, without downloading it

    with container.open("my_data.txt") as fp:
        data = np.loadtxt(fp)

    # Working with a project

    my_proj = Project('MyProject', username="xyzabc")
    container = my_proj.get_container("MyContainer")

    # Listing all your projects

    archive = Archive(username="xyzabc")
    projects = archive.projects
    container = archive.find_container("MyContainer")  # will search through all projects

"""

from __future__ import division
import getpass
import os
from keystoneauth1.identity import v3
from keystoneauth1 import session
from keystoneauth1.exceptions.auth import AuthorizationFailure
from keystoneauth1.extras._saml2 import V3Saml2Password
from keystoneclient.v3 import client as ksclient
import swiftclient.client as swiftclient
from swiftclient.exceptions import ClientException
try:
    from pathlib import Path
except ImportError:
    from pathlib2 import Path  # Python 2 backport
import requests
import logging

__version__ = "0.6.0"

OS_AUTH_URL = 'https://pollux.cscs.ch:13000/v3'
OS_IDENTITY_PROVIDER = 'cscskc'
OS_IDENTITY_PROVIDER_URL = 'https://kc.cscs.ch/auth/realms/cscs/protocol/saml/'

logger = logging.getLogger("hbp_archive")

def scale_bytes(value, units):
    """Convert a value in bytes to a different unit"""
    allowed_units = {
        'bytes': 1,
        'kB': 1024,
        'MB': 1048576,
        'GB': 1073741824,
        'TB': 1099511627776
    }
    if units not in allowed_units:
        raise ValueError("Units must be one of {}".format(list(allowed_units.keys())))
    scale = allowed_units[units]
    return value / scale


class File(object):
    """
    A representation of a file in a container.
    """

    def __init__(self, name, bytes, content_type, hash, last_modified, container=None):
        self.name = name
        self.bytes = bytes
        self.content_type = content_type
        self.hash = hash
        self.last_modified = last_modified
        self.container = container

    def __str__(self):
        return "'{}'".format(self.name)

    def __repr__(self):
        return "'{}'".format(self.name)

    @property
    def dirname(self):
        return os.path.dirname(self.name)

    @property
    def basename(self):
        return os.path.basename(self.name)

    def download(self, local_directory, with_tree=True, overwrite=False):
        """Download this file to a local directory.
           The following parameters may be specified:

        local_directory : string
            Local directory path where file is to be saved.
        with_tree : boolean, optional
            Specify if directory structure of file is to be retained.
        overwrite : boolean, optional
            Specify if any already existing file should be overwritten.
        """
        if self.container:
            self.container.download(self.name, local_directory=local_directory, with_tree=with_tree, overwrite=overwrite)
        else:
            raise Exception("Parent container not known, unable to download")

    def read(self, decode='utf-8', accept=[]):
        """Read and return the contents of this file in the container.

        See the docstring for `Container.read()` for an explanation of the arguments.
        """
        if self.container:
            return self.container.read(self.name, decode=decode, accept=accept)
        else:
            raise Exception("Parent container not known, unable to read file contents")

    def move(self, target_directory, new_name=None, overwrite=False):
        """Move this file to the specified directory.
           The following parameters may be specified:

        target_directory : string
            Target directory where the file is to be moved.
        new_name : string, optional
            New name to be assigned to file (including extension, if any).
        overwrite : boolean, optional
            Specify if any already existing file should be overwritten.
        """
        if self.container:
            self.container.move(self.name, target_directory=target_directory, new_name=new_name, overwrite=overwrite)
        else:
            raise Exception("Parent container not known, unable to move")

    def rename(self, new_name, overwrite=False):
        """Rename this file within the source directory.
           The following parameters may be specified:

        new_name : string
            New name to be assigned to file (including extension, if any).
        overwrite : boolean, optional
            Specify if any already existing file should be overwritten.
        """
        self.move(target_directory=os.path.dirname(self.name), new_name=new_name, overwrite=overwrite)

    def copy(self, target_directory, new_name=None, overwrite=False):
        """Copy this file to specified directory.
           The following parameters may be specified:

        target_directory : string
            Target directory where the file is to be copied.
        new_name : string, optional
            New name to be assigned to file (including extension, if any).
        overwrite : boolean, optional
            Specify if any already existing file at target location should be overwritten.
        """
        self.container.copy(self.name, target_directory=os.path.dirname(self.name), new_name=new_name, overwrite=overwrite)

    def delete(self):
        """Delete this file."""
        self.container.delete(self.name)

    def size(self, units='bytes'):
        """Return the size of this file in the requested unit (default bytes)."""
        return scale_bytes(self.bytes, units)


class Container(object):
    """
    A representation of a storage container,
    with methods for listing, counting, downloading, etc.
    the files it contains.

    A CSCS account is needed to use this class.
    """

    def __init__(self, container, username, token=None, project=None):
        if project is None:
            archive = Archive(username, token=token)
            project = archive.find_container(container).project
        elif isinstance(project, str):
            project = Project(project, username=username, token=token)
        self.project = project
        self.name = container
        self._metadata = None

    def __str__(self):
        return "'{}/{}'".format(self.project, self.name)

    def __repr__(self):
        return "Container('{}', project='{}', username='{}')".format(
            self.name, self.project.name, self.project.archive.username)

    @property
    def metadata(self):
        """Metadata about the container"""
        if self._metadata is None:
            self._metadata = self.project._connection.head_container(self.name)
        return self._metadata

    def list(self):  # , content_type=None, newer_than=None, older_than=None):
        """List all files in the container."""
        self._metadata, contents = self.project._connection.get_container(self.name)
        return [File(container=self, **item) for item in contents]

    def get(self, file_path):
        """Return a File object for the file at the given path."""
        for f in self.list():  # very inefficient
            if f.name == file_path:
                return f
        raise ValueError("Path '{}' does not exist".format(file_path))

    def count(self):
        """Number of files in the container"""
        return int(self.metadata['x-container-object-count'])

    def size(self, units='bytes'):
        """Total size of all data in the container"""
        return scale_bytes(int(self.metadata['x-container-bytes-used']), units)

    def upload(self, local_paths, remote_directory="", overwrite=False):
        """Upload file(s) to the container.
           The following parameters may be specified:

        local_paths : string, list of strings
            Local path of file(s) to be uploaded.
        remote_directory : string, optional
            Remote directory path where data is to be uploaded. Default is root directory.
        overwrite : boolean, optional
            Specify if any already existing file at target should be overwritten.

        Note: Using the command-line "swift upload" will likely be faster since
              it uses a pool of threads to perform multiple uploads in parallel.
              It is thus recommended for bulk uploads.
        """
        if isinstance(local_paths, str):
            local_paths = [local_paths]
        remote_paths = []

        for path in local_paths:
            remote_path = os.path.join(remote_directory, os.path.basename(path))
            if not overwrite:
                try:
                    res = self.project._connection.head_object(self.name, remote_path)
                    raise IOError("Target file path already exists! Set `overwrite=True` to overwrite file.")
                except IOError as e:            # if file already exists
                    logging.error("File: {} not uploaded. Reason: {}".format(path, e))
                    return
                except ClientException as e:    # if file does not exist
                    pass
            with open(path, 'rb') as f:
                file_data = f.read()
                self.project._connection.put_object(self.name, remote_path, file_data)
                remote_paths.append(remote_path)
        return remote_paths

    def download(self, file_path, local_directory=".", with_tree=True, overwrite=False):
        """Download a file from the container.
           The following parameters may be specified:

        file_path : string
            Path of file to be downloaded.
        local_directory : string, optional
            Local directory path where file is to be saved.
        with_tree : boolean, optional
            Specify if directory structure of file is to be retained.
        overwrite : boolean, optional
            Specify if any already existing file should be overwritten.
        """
        # todo: allow file_path to be a File object
        headers, contents = self.project._connection.get_object(self.name, file_path)
        if with_tree:
            local_directory = os.path.join(os.path.abspath(local_directory),
                                           *os.path.dirname(file_path).split("/"))
        Path(local_directory).mkdir(parents=True, exist_ok=True)
        local_path = os.path.join(local_directory, os.path.basename(file_path))
        if not overwrite and os.path.exists(local_path):
            raise IOError("Destination file ({}) already exists! Set `overwrite=True` to overwrite file.".format(local_path))
        with open(local_path, "wb") as local:
            local.write(contents)
        return local_path
        # todo: check hash

    def read(self, file_path, decode='utf-8', accept=[]):
        """Read and return the contents of a file in the container.

        Files containing text will be decoded using the provided encoding (default utf-8).
        If you would like to force decoding, put the expected content type in 'accept'.
        If you would like to prevent any attempt at decoding, set `decode=False`.
        """
        text_content_types = ["application/json", ]
        headers, contents = self.project._connection.get_object(self.name, file_path)
        # todo: check hash
        content_type = headers["content-type"]
        ct_parts = content_type.split("/")
        if (ct_parts[0] == "text" or content_type in text_content_types or content_type in accept) and decode:
            return contents.decode(decode)
        else:
            return contents

    def copy(self, file_path, target_directory, new_name=None, overwrite=False):
        """Copy a file to the specified directory.
           The following parameters may be specified:

        file_path : string
            Path of file to be copied.
        target_directory : string
            Target directory where the file is to be copied.
        new_name : string, optional
            New name to be assigned to file (including extension, if any).
        overwrite : boolean, optional
            Specify if any already existing file should be overwritten.
        """
        if not new_name:
            new_name = os.path.basename(file_path)
        if not overwrite:
            try:
                res = self.project._connection.head_object(self.name, os.path.join(target_directory, new_name))
                raise IOError("Target file path already exists! Set `overwrite=True` to overwrite file.")
            except IOError as e:
                logging.error(e)
                return
            except ClientException as e:
                pass
        try:
            self.project._connection.copy_object(self.name, file_path, destination=os.path.join(self.name, target_directory, new_name))
            logging.info("Successfully copied the object")
        except ClientException as e:
            logging.info("Failed to copy the object with error: %s" % e)

    def move(self, file_path, target_directory, new_name=None, overwrite=False):
        """Move a file to the specified directory.
           The following parameters may be specified:

        file_path : string
            Path of file to be moved.
        target_directory : string
            Target directory where the file is to be moved.
        new_name : string, optional
            New name to be assigned to file (including extension, if any).
        overwrite : boolean, optional
            Specify if any already existing file should be overwritten.
        """
        if not new_name:
            new_name = os.path.basename(file_path)
        if not overwrite:
            try:
                res = self.project._connection.head_object(self.name, os.path.join(target_directory, new_name))
                raise IOError("Target file path already exists! Set `overwrite=True` to overwrite file.")
            except IOError as e:
                logging.error(e)
                return
            except ClientException as e:
                pass
        try:
            self.project._connection.copy_object(self.name, file_path, destination=os.path.join(self.name, target_directory, new_name))
            self.project._connection.delete_object(self.name, file_path)
            if os.path.dirname(file_path) == target_directory:
                logging.info("Successfully renamed the object")
            else:
                logging.info("Successfully moved the object")
        except ClientException as e:
            logging.error("Failed to move/rename the object with error: %s" % e)

    def delete(self, file_path):
        """Delete the specified file.
           The following parameter needs to be specified:

        file_path : string
            Path of file to be deleted.
        """
        try:
            self.project._connection.delete_object(self.name, file_path)
            logging.info("Successfully deleted the object")
        except ClientException as e:
            logging.error("Failed to delete the object with error: %s" % e)

    def copy_directory(self, directory_path, target_directory, new_name=None, overwrite=False):
        """Copy a directory to the specified directory location.
           The original tree structure of the directory will be maintained at
           the target location. The following parameters may be specified:

        directory_path : string
            Path of directory to be copied.
        target_directory : string
            Path of target directory where specified directory is to be copied.
        new_name : string, optional
            New name to be assigned to directory.
        overwrite : boolean, optional
            Specify if any already existing files at target location should be
            overwritten. If False (default value), then only non-conflicting
            files will be copied over.
        """
        if directory_path[-1] != '/':
            directory_path += '/'
        if not new_name:
            new_name = os.path.basename(directory_path)
        all_files = self.list()
        dir_files = [f for f in all_files if f.name.startswith(directory_path)]
        if not dir_files:
            raise Exception("Specified directory does not exist in this container!")
        else:
            logging.info("***** Directory Copy Details *****")
            for f in dir_files:
                logging.info("Filename: {}".format(f.name))
                self.copy(f.name, os.path.join(target_directory, new_name), overwrite=overwrite)

    def move_directory(self, directory_path, target_directory, new_name=None, overwrite=False):
        """Move a directory to the specified directory location.
           Can also be used to rename a directory.
           The original tree structure of the directory will be maintained at
           the target location. The following parameters may be specified:

        directory_path : string
            Path of directory to be copied.
        target_directory : string
            Path of target directory where specified directory is to be copied.
        new_name : string, optional
            New name to be assigned to directory.
        overwrite : boolean, optional
            Specify if any already existing files at target location should be
            overwritten. If False (default value), then only non-conflicting
            files will be copied over.
        """
        if directory_path[-1] != '/':
            directory_path += '/'
        if not new_name:
            new_name = os.path.basename(directory_path)
        all_files = self.list()
        dir_files = [f for f in all_files if f.name.startswith(directory_path)]
        if not dir_files:
            raise Exception("Specified directory does not exist in this container!")
        else:
            logging.info("***** Directory Move Details *****")
            for f in dir_files:
                logging.info("Filename: {}".format(f.name))
                self.move(f.name, os.path.join(target_directory, new_name), overwrite=overwrite)

    def delete_directory(self, directory_path):
        """Delete the specified directory (and its contents).
           The following parameter needs to be specified:

        directory_path : string
            Path of directory to be deleted.
        """
        if directory_path[-1] != '/':
            directory_path += '/'
        all_files = self.list()
        dir_files = [f for f in all_files if f.name.startswith(directory_path)]
        if not dir_files:
            raise Exception("Specified directory does not exist in this container!")
        else:
            logging.info("***** Directory Delete Details *****")
            for f in dir_files:
                logging.info("Filename: {}".format(f.name))
                self.delete(f.name)

    def access_control(self, show_usernames=True):
        """List the users that have access to this container."""
        acl = {}
        for key in ("read", "write"):
            item = self.metadata.get('x-container-{}'.format(key), [])
            if item:
                item = item.split(",")
            acl[key] = item
        if show_usernames:  # map user id to username
            user_id_map = self.project.users
            for key in ("read", "write"):
                is_public = False
                user_ids = []
                for item in acl[key]:
                    if item in ('.r:*', '.rlistings'):
                        is_public = True
                    else:
                        user_ids.append(item.split(":")[1])  # each item is "project:user_id"
                acl[key] = [user_id_map.get(user_id, user_id) for user_id in user_ids]
                if is_public:
                    acl[key].append("PUBLIC")
        return acl

    def grant_access(self, username, mode='read'):
        """
        Give read or write access to the given user.

        Use restricted to Superusers/Operators.
        """
        name_map = {v: k for k, v in self.project.users.items()}
        user_id = name_map[username]
        new_acl = self.access_control(show_usernames=False)[
            mode] + ["{}:{}".format(self.project.id, user_id)]
        headers = {"x-container-{}".format(mode): ",".join(new_acl)}
        response = self.project._connection.post_container(self.name, headers)
        self._metadata = None  # needs to be refreshed


class PublicContainer(object):  # todo: figure out inheritance relationship with Container
    """
    A representation of a public storage container,
    with methods for listing, counting, downloading, etc.
    the files it contains.

    Note: This class only permits read-only operations. For other features,
    you may access a public container via the `Container` class.
    """

    def __init__(self, url):
        self.url = url
        self.name = url.split("/")[-1]
        self.project = None
        self._content_list = None

    def __str__(self):
        return self.url

    def __repr__(self):
        return "PublicContainer('{}')".format(self.url)

    def list(self):  # todo: allow refreshing, in case contents have changed
        if self._content_list is None:
            response = requests.get(self.url, headers={"Accept": "application/json"})
            if response.ok:
                self._content_list = [File(container=self, **entry) for entry in response.json()]
            else:
                raise Exception(response.content)
        return self._content_list

    def get(self, file_path):
        """Return a File object for the file at the given path."""
        for f in self.list():  # very inefficient
            if f.name == file_path:
                return f
        raise ValueError("Path '{}' does not exist".format(file_path))

    def count(self):
        """Number of files in the container"""
        return len(self.list())

    def size(self, units='bytes'):
        """Total size of all data in the container"""
        total_bytes = sum(f.bytes for f in self.list())
        return scale_bytes(total_bytes, units)

    def download(self, file_path, local_directory=".", with_tree=True, overwrite=False):
        """Download a file from the container. The following parameters may be specified:

        file_path : string
            Path of file to be downloaded.
        local_directory : string, optional
            Local directory path where file is to be saved.
        with_tree : boolean, optional
            Specify if directory structure of file is to be retained.
        overwrite : boolean, optional
            Specify if any already existing file should be overwritten.
        """
        # todo: allow file_path to be a File object
        # todo: implement direct streaming to file without
        #       storing copy in memory, see for example
        #       https://stackoverflow.com/questions/13137817/how-to-download-image-using-requests
        response = requests.get(self.url + "/" + file_path)
        if response.ok:
            contents = response.content
        else:
            raise Exception(response.content)
        if with_tree:
            local_directory = os.path.join(os.path.abspath(local_directory),
                                           *os.path.dirname(file_path).split("/"))
        Path(local_directory).mkdir(parents=True, exist_ok=True)
        local_path = os.path.join(local_directory, os.path.basename(file_path))
        if not overwrite and os.path.exists(local_path):
            raise IOError("Destination file ({}) already exists! Set `overwrite=True` to overwrite file.".format(local_path))
        with open(local_path, 'wb') as local:
            local.write(contents)
        return local_path
        # todo: check hash

    def read(self, file_path, decode='utf-8', accept=[]):
        """Read and return the contents of a file in the container.

        Files containing text will be decoded using the provided encoding (default utf-8).
        If you would like to force decoding, put the expected content type in 'accept'.
        If you would like to prevent any attempt at decoding, set `decode=False`.
        """
        text_content_types = ["application/json", ]
        response = requests.get(self.url + "/" + file_path)
        if response.ok:
            contents = response.content
            headers = response.headers
        else:
            raise Exception(response.content)
        # todo: check hash
        content_type = headers["Content-Type"]
        if ";" in content_type:
            content_type, encoding = content_type.split(";")
            # todo: handle conflict between encoding and "decode" argument
        ct_parts = content_type.split("/")
        if (ct_parts[0] == "text" or content_type in text_content_types or content_type in accept) and decode:
            return contents.decode(decode)
        else:
            return contents


class Project(object):
    """
    A representation of a Project,
    with methods for listing containers and users
    associated with that project.
    """

    def __init__(self, project, username, token=None, archive=None):
        if archive is None:
            archive = Archive(username, token=token)
        ks_project = archive._ks_projects[project]
        self.archive = archive
        self.id = ks_project.id
        self.name = ks_project.name
        self._session = None
        self.__connection = None
        self._containers = None
        self._user_id_map = None

    def __str__(self):
        return self.name

    def __repr__(self):
        return "Project('{}', username='{}')".format(self.name, self.archive.username)

    @property
    def _connection(self):
        if self.__connection is None:
            if self._session is None:
                self._set_scope()
            self.__connection = swiftclient.Connection(session=self._session)
        return self.__connection

    def _set_scope(self):
        auth = v3.Token(auth_url=OS_AUTH_URL,
                        token=self.archive._session.get_token(),
                        project_id=self.id)
        self._session = session.Session(auth=auth)

    def _get_container_info(self):
        try:
            headers, containers = self._connection.get_account()
        except ClientException:
            containers = []
        return containers

    def get_container(self, name):
        if name not in self.containers:
            container = Container(name, self.archive.username, project=self)
            container.metadata  # check that we can connect to the container
            self._containers[name] = container
        return self.containers[name]

    @property
    def containers(self):
        """Containers you have access to in this project."""
        if self._containers is None:
            self._containers = {name: Container(name, username=self.archive.username, project=self)
                                for name in self.container_names if not name.endswith("_versions")}
        return self._containers

    @property
    def container_names(self):
        return [item['name'] for item in self._get_container_info()]

    @property
    def users(self):
        """Return a mapping from usernames to user ids"""
        if self._user_id_map is None:
            self._user_id_map = {}
            proj_info = self.containers.get('project_info', None)
            if proj_info:
                user_id_doc = proj_info.read('user_ids', accept=['application/octet-stream'])
                in_user_list = False
                for line in user_id_doc.split("\n"):
                    if line:
                        if line.startswith("# user ids"):
                            in_user_list = True
                        elif in_user_list:
                            user_id, username = line.split(" ")
                            self._user_id_map[user_id] = username
        return self._user_id_map


class Archive(object):
    """
    A representation of the Human Brain Project archival storage (Pollux SWIFT) at CSCS,
    with methods for listing the projects you are associated with,
    and for searching for containers by name.
    """

    def __init__(self, username, token=None):
        self.username = username
        if token:
            auth = v3.Token(auth_url=OS_AUTH_URL, token=token)
        else:
            pwd = os.environ.get('CSCS_PASS')
            if not pwd:
                pwd = getpass.getpass("Password: ")
            auth = V3Saml2Password(auth_url=OS_AUTH_URL,
                                   identity_provider=OS_IDENTITY_PROVIDER,
                                   protocol='mapped',
                                   identity_provider_url=OS_IDENTITY_PROVIDER_URL,
                                   username=username,
                                   password=pwd)

        self._session = session.Session(auth=auth)
        self._client = ksclient.Client(session=self._session, interface='public')
        try:
            self.user_id = self._session.get_user_id()
        except AuthorizationFailure:
            raise Exception("Couldn't authenticate! Incorrect username.")
        except IndexError:
            raise Exception("Couldn't authenticate! Incorrect password.")
        self._ks_projects = {ksprj.name: ksprj
                             for ksprj in self._client.projects.list(user=self.user_id)}
        self._projects = None

    @property
    def projects(self):
        """Projects you have access to"""
        if self._projects is None:
            self._projects = {ksprj_name: Project(ksprj_name, username=self.username, archive=self)
                              for ksprj_name in self._ks_projects}
        return self._projects

    def find_container(self, container):
        """
        Search through all projects for the container with the given name.

        Return a Container object.

        If the container is not found, raise an Exception
        """
        for project in self.projects.values():
            try:
                return project.get_container(container)
            except ClientException:
                pass
        raise ValueError(
            "Container {} not found. Please check your access permissions.".format(container))
