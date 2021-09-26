import collections
import functools
import logging
import random
import re
import string
from typing import Dict, Optional, Tuple, Union, List

from pyzabbix import ZabbixAPI, ZabbixAPIException


class ZabbixConn(object):
    """
    Zabbix connector class

    Defines methods for managing Zabbix users and groups

    """

    def __init__(self, config, ldap_conn):
        self.conn = None
        self.ldap_conn = ldap_conn
        self.server = config.zbx_server
        self.username = config.zbx_username
        self.password = config.zbx_password
        self.alldirusergroup = config.zbx_alldirusergroup
        self.auth = config.zbx_auth
        self.dryrun = config.dryrun
        self.nocheckcertificate = config.zbx_ignore_tls_errors
        self.preserve_accountids = config.ldap_accountids
        self.ldap_groups = config.ldap_groups
        self.ldap_media = config.ldap_media
        self.media_opt = config.media_opt
        self.deleteorphans = config.zbx_deleteorphans
        self.removeabsent = config.zbx_removeabsent
        self.media_name = config.media_name
        self.user_opt = config.user_opt
        if self.nocheckcertificate:
            from requests.packages.urllib3 import disable_warnings
            disable_warnings()

        if config.ldap_wildcard_search:
            self.ldap_groups = ldap_conn.get_groups_with_wildcard(self.ldap_groups)

        # Use logger to log information
        self.logger = logging.getLogger(self.__class__.__name__)

        self.username_attribute = "alias"

    def connect(self) -> bool:
        """
        Establishes a connection to the Zabbix server

        Raises:
            SystemExit

        """

        if self.auth == "webform":
            self.conn = ZabbixAPI(self.server)
        elif self.auth == "http":
            self.conn = ZabbixAPI(self.server, use_authenticate=False)
            self.conn.session.auth = (self.username, self.password)

        else:
            raise SystemExit('api auth method not implemented: %s' % self.conn.auth)

        if self.nocheckcertificate:
            self.conn.session.verify = False

        try:
            self.conn.login(self.username, self.password)
        except ZabbixAPIException as e:
            raise SystemExit('Cannot login to Zabbix server: %s' % e)

        self.logger.info("Connected to Zabbix API Version %s" % self.conn.api_version())
        if self.conn.api_version() >= "5.4":
            self.username_attribute = "username"
        return True

    def get_users(self) -> List[dict]:
        return self.conn.user.get(output='extend')

    def get_users_names(self) -> List[str]:
        """
        Retrieves the existing Zabbix users

        Returns:
            A list of the existing Zabbix users

        """
        result = self.get_users()

        if self.preserve_accountids:
            users = [user[self.username_attribute] for user in result]
        else:
            users = [user[self.username_attribute].lower() for user in result]
        return users

    def get_mediatype_id(self, name: str) -> Optional[str]:
        """
        Retrieves the mediatypeid by name

        Args:
            name (str): Zabbix media type name

        Returns:
            The mediatypeid for specified media type name

        """
        result = self.conn.mediatype.get(filter={'name': name.strip()})

        if len(result) < 1:
            raise Exception(f"No such media for {name} found, check your configuration")
        elif len(result) > 1:
            raise Exception(f"Ambiguous media '{name}' found, {len(result)} different medias")

        if result:
            mediatypeid = result[0]['mediatypeid']
        else:
            mediatypeid = None

        return mediatypeid

    def get_user_id(self, user: str) -> Optional[str]:
        """
        Retrieves the userid of a specified user

        Args:
            user (str): The Zabbix username to lookup

        Returns:
            The userid of the specified user

        """
        result = self.get_users()

        userids = [u['userid'] for u in result if u[self.username_attribute].lower() == user]
        if len(userids) == 1:
            return userids[0]
        else:
            return None

    def get_groups(self):
        """
        Retrieves the existing Zabbix groups

        Returns:
            A dict of the existing Zabbix groups and their group ids

        """
        result = self.conn.usergroup.get(output='extend')

        groups = [{'name': group['name'], 'usrgrpid': group['usrgrpid']} for group in result]

        return groups

    def get_group_members(self, groupid):
        """
        Retrieves group members for a Zabbix group

        Args:
            groupid (int): The group id

        Returns:
            A list of the Zabbix users for the specified group id

        """
        result = self.conn.user.get(output='extend', usrgrpids=groupid)

        users = [user[self.username_attribute] for user in result]

        return users

    def create_group(self, group):
        """
        Creates a new Zabbix group

        Args:
            group (str): The Zabbix group name to create

        Returns:
            The groupid of the newly created group

        """
        result = self.conn.usergroup.create(name=group)

        groupid = result['usrgrpids'].pop()

        return groupid

    def create_user(self, user: Dict, groupid: int, user_opt: Dict[str, str], password: str, role_id: Optional[int]):
        """
        Creates a new Zabbix user

        Args:
            user     (dict): A dict containing the user details
            groupid   (int): The groupid for the new user
            user_opt (dict): User options
            password  (str): The user password
            role_id   (int): Numeric id of the role for the user
        """
        user_settings = {'autologin': 0, 'usrgrps': [{'usrgrpid': str(groupid)}], 'passwd': password}

        for opt, value in user_opt.items():
            if opt == "show_password":
                continue
            else:
                user_settings[opt] = value

        if role_id:
            if self.conn.api_version() >= "5.2":
                user_settings['roleid'] = int(role_id)
            else:
                user_settings['type'] = int(role_id)

        user.update(user_settings)
        result = self.conn.user.create(user)
        return result

    def delete_user(self, user):
        """
        Deletes Zabbix user

        Args:
            user (string): Zabbix username

        """
        userid = self.get_user_id(user)

        result = self.conn.user.delete(userid)

        return result

    def update_user(self, user: str, group_id: int):
        """
        Adds an existing Zabbix user to a group

        Args:
            user    (dict): A dict containing the user details
            group_id  (int): The groupid to add the user to

        """
        userid = self.get_user_id(user)

        result = None
        if self.conn.api_version() >= "3.4":
            members = self.conn.usergroup.get(usrgrpids=[str(group_id)], selectUsers='extended')
            group_users = members[0]['users']
            user_ids = set()
            for u in group_users:
                user_ids.add(u['userid'])
            user_ids.add(str(userid))

            if not self.dryrun:
                result = self.conn.usergroup.update(usrgrpid=str(group_id), userids=list(user_ids))
        else:
            if not self.dryrun:
                result = self.conn.usergroup.massadd(usrgrpids=[str(group_id)], userids=[str(userid)])

        return result

    def remove_user(self, user: str, group_id: int):
        """
        Remove existing Zabbix user from group

        Args:
            user    (dict): A dict containing the user details
            group_id  (int): The groupid to add the user to

        """
        userid = self.get_user_id(user)

        result = None
        if self.conn.api_version() >= "3.4":
            members = self.conn.usergroup.get(usrgrpids=[str(group_id)], selectUsers='extended')
            group_users = members[0]['users']
            user_ids = set()
            for u in group_users:
                if u['userid'] != userid:
                    user_ids.add(u['userid'])
            if not self.dryrun:
                result = self.conn.usergroup.update(usrgrpid=str(group_id), userids=list(user_ids))

        return result

    def update_media(self, user: str, description: str, sendto: str, media_opt: list):
        """
        Adds media to an existing Zabbix user

        Args:
            user        (dict): A dict containing the user details
            description  (str): A string containing Zabbix media description
            sendto       (str): A string containing address, phone number, etc...
            media_opt    (dict): Media options

        """

        userid = self.get_user_id(user)
        mediatypeid = self.get_mediatype_id(description)

        if mediatypeid:
            media_defaults = {
                'mediatypeid': mediatypeid,
                'sendto': sendto,
                'active': '0',
                'severity': '63',
                'period': '1-7,00:00-24:00'
            }
            media_defaults.update(media_opt)

            for unwanted_attrib in ["description", "name", "onlycreate"]:
                if unwanted_attrib in media_defaults:
                    del media_defaults[unwanted_attrib]

            if self.conn.api_version() >= "3.4":
                result = self.conn.user.update(userid=str(userid), user_medias=[media_defaults])
            else:
                self.delete_media_by_description(user, description)
                result = self.conn.user.updatemedia(users=[{"userid": str(userid)}], medias=media_defaults)
        else:
            result = None

        return result

    def delete_media_by_description(self, user: str, description: str):
        """
        Remove all media from user (with specific mediatype)

        Args:
            user        (dict): A dict containing the user details
            description  (str): A string containing Zabbix media description

        """

        userid = self.get_user_id(user)
        mediatypeid = self.get_mediatype_id(description)

        if mediatypeid:
            user_full = self.conn.user.get(output="extend", userids=userid, selectMedias=["mediatypeid", "mediaid"])
            media_ids = [int(u['mediaid']) for u in user_full[0]['medias'] if u['mediatypeid'] == mediatypeid]

            if media_ids:
                self.logger.info('Remove other exist media from user %s (type=%s)' % (user, description))
                for media_id in media_ids:
                    self.conn.user.deletemedia(media_id)

    def create_missing_groups(self):
        """
        Creates any missing LDAP groups in Zabbix

        """

        groups = []
        for group_spec in self.ldap_groups:
            name, _ = self._get_group_spec(group_spec)
            groups.append(name)

        missing_groups = set(groups) - set([g['name'] for g in self.get_groups()])

        for eachGroup in missing_groups:
            self.logger.info('Creating Zabbix group %s' % eachGroup)
            if not self.dryrun:
                grpid = self.create_group(eachGroup)
                self.logger.info('Group %s created with groupid %s' % (eachGroup, grpid))

    def convert_severity(self, severity):

        converted_severity = severity.strip()

        if re.match(r"\d+", converted_severity):
            return converted_severity

        sev_entries = collections.OrderedDict({
            "Disaster": "0",
            "High": "0",
            "Average": "0",
            "Warning": "0",
            "Information": "0",
            "Not Classified": "0",
        })

        for sev in converted_severity.split(","):
            sev = sev.strip()
            if sev not in sev_entries:
                raise Exception("wrong argument: %s" % sev)
            sev_entries[sev] = "1"

        str_bitmask = ""
        for sev, digit in sev_entries.items():
            str_bitmask += digit

        converted_severity = str(int(str_bitmask, 2))
        self.logger.debug('Converted severity "%s" to "%s"' % (severity, converted_severity))

        return converted_severity

    def sync_users(self):
        """
        Syncs Zabbix with LDAP users
        """

        self.ldap_conn.connect()
        if self.alldirusergroup:
            zabbix_alldirusergroup_id = []
            for g in self.get_groups():
                if g['name'] == self.alldirusergroup:
                    zabbix_alldirusergroup_id.append(g['usrgrpid'])
            zabbix_alldirusergroup_id.append(g['usrgrpid'])
            zabbix_alldirusergroup_id = zabbix_alldirusergroup_id.pop()

            zabbix_alldirusergroup_users = self.get_group_members(zabbix_alldirusergroup_id)
        else:
            zabbix_alldirusergroup_id = None
            zabbix_alldirusergroup_users = []

        for group_spec in self.ldap_groups:

            group_name, role_id = self._get_group_spec(group_spec)

            self.logger.info('Processing group >>>%s<<<...' % group_name)
            zabbix_all_users = self.get_users_names()

            ldap_users = {}
            if self.preserve_accountids:
                ldap_users = {k: v for k, v in self.ldap_conn.get_group_members(group_name).items()}
            else:
                ldap_users = {k.lower(): v for k, v in self.ldap_conn.get_group_members(group_name).items()}

            # Do nothing if LDAP group contains no users and "--delete-orphans" is not specified
            if not ldap_users and not self.deleteorphans and not self.removeabsent:
                self.logger.info('Done for group %s. Nothing to do' % group_name)
                continue

            zabbix_group_id = []
            for g in self.get_groups():
                if g['name'] == group_name:
                    zabbix_group_id.append(g['usrgrpid'])

            zabbix_group_id = zabbix_group_id.pop()

            zabbix_group_users = self.get_group_members(zabbix_group_id)

            missing_users = set(ldap_users.keys()) - set(zabbix_group_users)
            # Add missing users
            self.logger.info('Syncing users...')
            for each_user in missing_users:
                # Create new user if it does not exists already
                if each_user not in zabbix_all_users:
                    random_passwd = ''.join(random.sample(string.ascii_letters + string.digits, 32))
                    for opt, value in self.user_opt.items():
                        if opt == "show_password" and value.lower() == "true":
                            self.logger.info(f"Created user {each_user}, start password" +
                                             f" {random_passwd} and membership of Zabbix group >>{group_name}<<")
                        else:
                            self.logger.info(
                                f"Created user {each_user} and membership of Zabbix group >>{group_name}<<")
                    user = {self.username_attribute: each_user}

                    if self.ldap_conn.get_user_givenName(ldap_users[each_user]) is None:
                        user['name'] = ''
                    else:
                        user['name'] = self.ldap_conn.get_user_givenName(ldap_users[each_user]).decode('utf8')
                    if self.ldap_conn.get_user_sn(ldap_users[each_user]) is None:
                        user['surname'] = ''
                    else:
                        user['surname'] = self.ldap_conn.get_user_sn(ldap_users[each_user]).decode('utf8')

                    if not self.dryrun:
                        self.create_user(user=user, groupid=zabbix_group_id, user_opt=self.user_opt,
                                         password=random_passwd, role_id=role_id)
                    zabbix_all_users.append(each_user)
                else:
                    # Update existing user to be member of the group
                    self.logger.info('Updating user "%s", adding to group "%s"' % (each_user, group_name))
                    if not self.dryrun:
                        self.update_user(each_user, zabbix_group_id)

            # Add users to Zabbix all directory users
            if zabbix_alldirusergroup_id:
                self.logger.info('Adding users to group %s...' % self.alldirusergroup)
                missing_allusers = set(list(ldap_users.keys())) - set(zabbix_alldirusergroup_users)
                for each_user in missing_allusers:
                    self.logger.info('Updating user "%s", adding to group "%s"' % (each_user, self.alldirusergroup))
                    self.update_user(each_user, zabbix_alldirusergroup_id)

            # Handle any extra users in the group
            self.logger.info('Handle extra users...')
            absent_users = set(zabbix_group_users) - set(list(ldap_users.keys()))
            count = 0
            if absent_users:
                for each_user in absent_users:
                    if each_user not in zabbix_alldirusergroup_users:
                        continue
                    count = count + 1
                    if count == 1:
                        self.logger.info('Users in group %s which are not found in LDAP group:' % group_name)
                    if self.deleteorphans:
                        self.logger.info('Deleting user: "%s"' % each_user)
                        if not self.dryrun:
                            try:
                                self.delete_user(each_user)
                            except Exception as e:
                                self.logger.error('Error deleting user "%s"' % each_user)
                                self.logger.error(e)
                    else:
                        self.logger.info('User not in ldap group "%s"' % each_user)
                        if self.removeabsent:
                            self.logger.info('Removing user from group: "%s"...' % each_user)
                            if not self.dryrun:
                                self.remove_user(each_user, zabbix_group_id)
            # if count == 0:
            #     self.logger.info('No absent users')

            # update users media
            self.logger.info('Updating media for users...')
            onlycreate = False
            media_opt_filtered = []
            for elem in self.media_opt:
                if elem[0] == "onlycreate" and elem[1].lower() == "true":
                    onlycreate = True
                if elem[0] == "severity":
                    media_opt_filtered.append(
                        (elem[0], self.convert_severity(elem[1]))
                    )
                else:
                    media_opt_filtered.append(elem)

            if onlycreate:
                self.logger.info("Add media only on newly created users for group >>>%s<<<" % group_name)
                zabbix_group_users = missing_users
            else:
                self.logger.info("Update media on all users for group >>>%s<<<" % group_name)
                zabbix_group_users = self.get_group_members(zabbix_group_id)

            for each_user in set(zabbix_group_users):
                if not self.preserve_accountids:
                    each_user = each_user.lower()

                if self.ldap_media:
                    if self.ldap_conn.get_user_media(ldap_users[each_user], self.ldap_media):
                        sendto = self.ldap_conn.get_user_media(ldap_users[each_user], self.ldap_media).decode("utf8")
                    else:
                        sendto = self.ldap_conn.get_user_media(ldap_users[each_user], self.ldap_media)

                    if each_user not in absent_users and sendto is not None and not self.dryrun:
                        self.logger.info(
                            '>>> Updating/create user media for "%s", update "%s"' % (each_user, self.media_name))
                        self.update_media(each_user, self.media_name, sendto, media_opt_filtered)
                else:
                    self.logger.info('>>> Ignoring media for "%s" because of configuration' % each_user)
            self.logger.info('Done for group %s' % group_name)

        self.ldap_conn.disconnect()
        self.logger.info('Done!')

    def _get_group_spec(self, group_spec: str) -> Union[str, Tuple[str, str]]:
        m = re.match(r"^(.+):(\d+)$", group_spec)
        if m:
            group_name = m.group(1).strip()
            role_id = m.group(2).strip()
            return group_name, role_id
        else:
            group_name = group_spec
            return group_name, None
