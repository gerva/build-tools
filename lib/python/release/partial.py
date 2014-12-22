from paths import makeCandidatesDir, makeReleasesDir
from platforms import buildbot2ftp
from download import url_exists


class Partial(object):
    """Models a partial update, used in release_sanity"""
    def __init__(self, product, version, build_number, protocol, server):
        self.product = product
        self.version = version
        self.build_number = build_number
        self.server = server
        self.protocol = protocol

    def __str__(self):
        name = [self.product, self.version]
        # e.g. firefox 123.0b4
        if self.build_number is not None:
            name.extend(['build', int(self.build_number)])
            # e.g. firefox 123.0b4 build 5
        return " ".join(name)

    def _is_from_candidates_dir(self):
        return self.build_number is None

    def complete_mar_name(self):
        return '%s-%s.complete.mar' % (self.product, self.version)

    def complete_mar_url(self, platform):
        ftp_platform = buildbot2ftp(platform)
        url = makeReleasesDir(self.product, self.version)
        if self._is_from_candidates_dir():
            url = makeCandidatesDir(product=self.product,
                                    version=self.version,
                                    buildNumber=self.build_number,
                                    nightlyDir='candidates',
                                    server=self.server,
                                    protocol=self.protocol)
        return "/".join([url, 'update', ftp_platform,
                         'en-US', self.complete_mar_url()])

    def exists(self, platform):
        return url_exists(self.complete_mar_url(platform))
