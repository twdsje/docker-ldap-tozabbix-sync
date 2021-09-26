import ldap
import ldap.filter
import logging


class LDAPConn(object):
    """
    LDAP connector class

    Defines methods for retrieving users and groups from LDAP server.

    """

    def __init__(self, config):
        self.conn = None
        self.disabled_filter = config.ad_filterdisabled
        self.uri = config.ldap_uri
        self.base = config.ldap_base
        self.ldap_accountids = config.ldap_accountids
        self.ldap_user = config.ldap_user
        self.ldap_pass = config.ldap_passwd
        self.ldap_type = config.ldap_type
        self.group_member_attribute = config.ldap_group_member_attribute
        self.group_filter = config.ldap_group_filter
        self.uid_attribute = config.ldap_uid_attribute
        self.active_directory = config.ldap_active_directory
        self.recursive = config.ldap_recursive
        if self.recursive and self.active_directory:
            self.memberof_filter = config.ldap_memberof_filter
        self.skipdisabled = config.ldap_skipdisabled
        self.user_filter = config.ldap_user_filter
        self.verbose = config.verbose
        self.openldap_type = config.openldap_type

        self.logger = logging.getLogger(self.__class__.__name__)
        # Log from pyldap
        log = logging.getLogger('ldap')
        if self.verbose:
            log.setLevel(logging.DEBUG)
            ldap.set_option(ldap.OPT_DEBUG_LEVEL, 4095)

        if config.ldap_ignore_tls_errors:
            ldap.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_NEVER)

    def connect(self):
        """
        Establish a connection to the LDAP server.

        Raises:
            SystemExit

        """
        self.conn = ldap.initialize(self.uri)
        self.conn.set_option(ldap.OPT_REFERRALS, ldap.OPT_OFF)

        try:
            self.conn.simple_bind_s(self.ldap_user, self.ldap_pass)
        except ldap.SERVER_DOWN as e:
            raise SystemExit('Cannot connect to LDAP server: %s' % e)

    def disconnect(self):
        """
        Disconnect from the LDAP server.

        """
        self.conn.unbind()

    def remove_ad_referrals(self, result: list):
        """
        Remove referrals from AD query result

        """
        return [i for i in result if i[0] is not None]

    def get_group_members(self, group: str):
        """
        Retrieves the members of an LDAP group

        Args:
            group (str): The LDAP group name

        Returns:
            A list of all users in the LDAP group

        """
        attrlist = [self.group_member_attribute]
        filter = self.group_filter % group
        self.logger.debug('Searching LDAP with filter >>>%s<<<' % filter)
        result = self.conn.search_s(base=self.base,
                                    scope=ldap.SCOPE_SUBTREE,
                                    filterstr=filter,
                                    attrlist=attrlist)

        if not result:
            self.logger.info('Unable to find group "%s" with filter "%s", skipping group' % (group, filter))
            return None

        # Get DN for each user in the group
        if self.active_directory:
            return self.get_group_members_active_directory(result)
        else:
            return self.get_group_members_ldap(result)

    def get_group_members_ldap(self, result: list):
        dn, users = result.pop()
        if not users:
            return {}
        final_listing = {}
        group_members = []
        # Get info for each user in the group
        for memberid in users[self.group_member_attribute]:
            memberid = memberid.decode("utf-8")

            if self.openldap_type == "groupofnames":
                filter = "(objectClass=*)"
                # memberid is user dn
                base = memberid
            else:

                # memberid is user attribute, most likely uid
                filter = self.user_filter % memberid
                base = self.base

            attrlist = [self.uid_attribute]

            # get the actual LDAP object for each group member
            self.logger.debug('Searching LDAP with filter >>>%s<<<' % filter)
            uid = self.conn.search_s(base=base,
                                     scope=ldap.SCOPE_SUBTREE,
                                     filterstr=filter,
                                     attrlist=attrlist)

            for item in uid:
                group_members.append(item)

            # Fill dictionary with usernames and corresponding DNs
            for item in group_members:
                dn = item[0]

                username = item[1][self.uid_attribute]
                user = ''.join(username[0].decode('utf-8'))

                final_listing[user] = dn

        return final_listing

    def get_group_members_active_directory(self, result: list):
        result = self.remove_ad_referrals(result)
        final_listing = {}

        for members in result:
            result_dn = members[0]
            result_attrs = members[1]
            group_members = []
            attrlist = [self.uid_attribute]
            if self.recursive:
                # Get a DN for all users in a group (recursive)
                # It's available only on domain controllers with Windows Server 2003 SP2 or later

                member_of_filter_dn = self.memberof_filter % result_dn

                if self.skipdisabled:
                    filter = "(&%s%s%s)" % (self.user_filter, member_of_filter_dn, self.disabled_filter)
                else:
                    filter = "(&%s%s)" % (self.user_filter, member_of_filter_dn)

                self.logger.debug('Searching LDAP with filter >>>%s<<<' % filter)
                uid = self.conn.search_s(base=self.base,
                                         scope=ldap.SCOPE_SUBTREE,
                                         filterstr=filter,
                                         attrlist=attrlist)

                for item in self.remove_ad_referrals(uid):
                    group_members.append(item)
            else:
                # Otherwise, just get a DN for each user in the group
                for member in result_attrs[self.group_member_attribute]:
                    if self.skipdisabled:
                        filter = "(&%s%s)" % (self.user_filter, self.disabled_filter)
                    else:
                        filter = "(&%s)" % self.user_filter

                    self.logger.debug('Searching LDAP with filter >>>%s<<<' % filter)
                    uid = self.conn.search_s(base=member.decode('utf8'),
                                             scope=ldap.SCOPE_BASE,
                                             filterstr=filter,
                                             attrlist=attrlist)
                    for item in uid:
                        group_members.append(item)
            # Fill dictionary with usernames and corresponding DNs
            for item in group_members:
                dn = item[0]
                username = item[1][self.uid_attribute]

                if self.ldap_accountids:
                    username = username[0].decode('utf8')
                else:
                    username = username[0].decode('utf8').lower()

                final_listing[username] = dn
        return final_listing

    def get_user_media(self, dn: str, ldap_media: list):
        """
        Retrieves the 'media' attribute of an LDAP user

        Args:
            dn (str): The LDAP distinguished name to lookup
            ldap_media (str): The name of the field containing the media address

        Returns:
            The user's media attribute value

        """
        attrlist = [ldap_media]

        result = self.conn.search_s(base=dn,
                                    scope=ldap.SCOPE_BASE,
                                    attrlist=attrlist)

        if not result:
            return None

        dn, data = result.pop()

        mail = data.get(ldap_media)

        if not mail:
            return None

        return mail.pop()

    def get_user_sn(self, dn: str):
        """
        Retrieves the 'sn' attribute of an LDAP user

        Args:
            dn (str): The LDAP distinguished name to lookup

        Returns:
            The user's surname attribute

        """
        attrlist = ['sn']

        result = self.conn.search_s(base=dn,
                                    scope=ldap.SCOPE_BASE,
                                    attrlist=attrlist)

        if not result:
            return None

        dn, data = result.pop()

        sn = data.get('sn')

        if not sn:
            return None

        return sn.pop()

    def get_user_givenName(self, dn: str):
        """
        Retrieves the 'givenName' attribute of an LDAP user

        Args:
            dn (str): The LDAP distinguished name to lookup

        Returns:
            The user's given name attribute

        """
        attrlist = ['givenName']

        result = self.conn.search_s(base=dn,
                                    scope=ldap.SCOPE_BASE,
                                    attrlist=attrlist)

        if not result:
            return None

        dn, data = result.pop()

        name = data.get('givenName')

        if not name:
            return None

        return name.pop()

    def get_groups_with_wildcard(self, groups_wildcard: str):

        filters = []
        for wildcard in groups_wildcard:
            self.logger.info("Search groups with wildcard: %s" % wildcard)
            filters.append(self.group_filter % wildcard)

        ldap_filter = "(| %s)" % (" ".join(filters))

        result_groups = []

        self.logger.debug('Searching LDAP with filter >>>%s<<<' % ldap_filter)
        result = self.conn.search_s(base=self.base,
                                    scope=ldap.SCOPE_SUBTREE,
                                    filterstr=ldap_filter)

        for group in result:
            # Skip refldap (when Active Directory used)
            # [0]==None
            if group[0]:
                group_name = group[1]['name'][0].decode()
                self.logger.info("Found group %s" % group_name)
                result_groups.append(group_name)

        if not result_groups:
            self.logger.info('Unable to find group "%s", skipping group wildcard' % groups_wildcard)

        return result_groups
