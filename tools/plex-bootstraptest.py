import argparse
import os
import platform
from glob import glob
from shutil import copyfile, rmtree
from subprocess import call
from time import time, sleep
from uuid import uuid4

from requests import codes

import plexapi
from plexapi.compat import which, makedirs
from plexapi.exceptions import BadRequest
from plexapi.utils import download, SEARCHTYPES

DOCKER_CMD = [
    'docker', 'run', '-d',
    '--name', 'plex-test-%(image_tag)s',
    '-p', '32400:32400/tcp',
    '-p', '3005:3005/tcp',
    '-p', '8324:8324/tcp',
    '-p', '32469:32469/tcp',
    '-p', '1900:1900/udp',
    '-p', '32410:32410/udp',
    '-p', '32412:32412/udp',
    '-p', '32413:32413/udp',
    '-p', '32414:32414/udp',
    '-e', 'TZ="Europe/London"',
    '-e', 'PLEX_CLAIM=%(claim_token)s',
    '-e', 'ADVERTISE_IP=http://%(advertise_ip)s:32400/',
    '-h', '%(hostname)s',
    '-e', 'TZ="%(timezone)s"',
    '-v', '%(destination)s/db:/config',
    '-v', '%(destination)s/transcode:/transcode',
    '-v', '%(destination)s/media:/data',
    'plexinc/pms-docker:%(image_tag)s'
]


def get_claim_token(myplex):
    """
    Returns a str, a new "claim-token", which you can use to register your new Plex Server instance to your account
    See: https://hub.docker.com/r/plexinc/pms-docker/, https://www.plex.tv/claim/

    Arguments:
        myplex (:class:`~plexapi.myplex.MyPlexAccount`)
    """
    response = myplex._session.get('https://plex.tv/api/claim/token.json', headers=myplex._headers(),
                                   timeout=plexapi.TIMEOUT)
    if response.status_code not in (200, 201, 204):
        codename = codes.get(response.status_code)[0]
        errtext = response.text.replace('\n', ' ')
        raise BadRequest('(%s) %s %s; %s' % (response.status_code, codename, response.url, errtext))
    return response.json()['token']


def get_ips():
    import socket
    return list(set([i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None)
                     if i[4][0] not in ('127.0.0.1', '::1') and not i[4][0].startswith('fe80:')]))


# Unfortunately plex ignore hardlinks created on OS X host machine, so we have to copy here
def cp(src, dst):
    if platform.system() == 'Darwin':
        copyfile(src, dst)
    else:
        os.link(src, dst)


if __name__ == '__main__':
    if which('docker') is None:
        print('Docker is required to be available')
        exit(1)

    default_ip = None
    available_ips = get_ips()
    if len(available_ips) > 0:
        default_ip = available_ips[0]

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--username', help='Your Plex username')
    parser.add_argument('--password', help='Your Plex password')
    parser.add_argument('--timezone', help='Timezone to set inside plex', default='UTC')
    parser.add_argument('--destination', help='Local path where to store all the media',
                        default=os.path.join(os.getcwd(), 'plex'))
    parser.add_argument('--advertise-ip', help='IP address which should be advertised by new Plex instance',
                        required=default_ip is None, default=default_ip)
    parser.add_argument('--docker-tag', help='Docker image tag to install', default='latest')
    parser.add_argument('--bootstrap-timeout', help='Timeout for each step of bootstrap, in seconds (default: '
                                                    '%(default)s)',
                        default=180, type=int)
    parser.add_argument('--server-name', help='Name for the new server', default='plex-test-docker-%s' % str(uuid4()))
    parser.add_argument('--accept-eula', help='Accept Plex`s EULA', default=False, action='store_true')
    parser.add_argument('--without-movies', help='Do not create Movies section', default=True, dest='with_movies',
                        action='store_false')
    parser.add_argument('--without-shows', help='Do not create TV Shows section', default=True, dest='with_shows',
                        action='store_false')
    parser.add_argument('--without-music', help='Do not create Music section', default=True, dest='with_music',
                        action='store_false')
    parser.add_argument('--without-photos', help='Do not create Photos section', default=True, dest='with_photos',
                        action='store_false')
    parser.add_argument('--without-album', help='Do not create Photo Album', default=True, dest='with_photo_album',
                        action='store_false')
    opts = parser.parse_args()
    print('I`m going to create a plex instance named %s with advertised ip "%s", be prepared!' % (opts.server_name,
                                                                                                  opts.advertise_ip))
    if call(['docker', 'pull', 'plexinc/pms-docker:%s' % opts.docker_tag]) != 0:
        print('Got an error when executing docker pull!')
        exit(1)
    account = plexapi.utils.getMyPlexAccount(opts)
    path = os.path.realpath(os.path.expanduser(opts.destination))
    arg_bindings = {
        'destination': path,
        'hostname': opts.server_name,
        'claim_token': get_claim_token(account),
        'timezone': opts.timezone,
        'advertise_ip': opts.advertise_ip,
        'image_tag': opts.docker_tag,
    }
    docker_cmd = [c % arg_bindings for c in DOCKER_CMD]
    exit_code = call(docker_cmd)

    if exit_code != 0:
        exit(exit_code)

    print('Let`s wait while the instance register in your plex account...')
    start_time = time()
    server = None
    while not server and (time() - start_time < opts.bootstrap_timeout):
        try:
            device = account.device(opts.server_name)
            server = device.connect()
            if opts.accept_eula:
                server.settings.get('acceptedEULA').set(True)
                server.settings.save()
        except Exception as e:
            print(e)
            sleep(1)

    if not server:
        print('Server didn`t appeared in your account after a lot of time, I have no idea what to do :( Dig into '
              'docker logs, check your internet connection, do something!')
        exit(1)

    print('Ok, I got the server instance, let`s download what you`re missing')

    expected_media_count = 0

    def get_tvshow_path(name, season, episode):
        return os.path.join(tvshows_path, name, 'S%02dE%02d.mp4' % (season, episode))

    if opts.with_movies or opts.with_shows:
        def get_movie_path(name, year):
            return os.path.join(movies_path, '%s (%d).mp4' % (name, year))

        media_stub_path = os.path.join(opts.destination, 'media', 'video_stub.mp4')
        if not os.path.isfile(media_stub_path):
            download('http://www.mytvtestpatterns.com/mytvtestpatterns/Default/GetFile?p=PhilipsCircleMP4', '',
                     filename='video_stub.mp4', savepath=os.path.join(opts.destination, 'media'), showstatus=True)

    sections = []
    if opts.with_movies:
        movies_path = os.path.join(opts.destination, 'media', 'Movies')
        makedirs(movies_path, exist_ok=True)

        required_movies = {
            'Elephants Dream': 2006,
            'Sita Sings the Blues': 2008,
            'Big Buck Bunny': 2008,
            'Sintel': 2010,
        }

        for name, year in required_movies.items():
            expected_media_count += 1
            if not os.path.isfile(get_movie_path(name, year)):
                cp(media_stub_path, get_movie_path(name, year))

        print('Finished with movies...')
        sections.append(dict(name='Movies', type='movie', location='/data/Movies', agent='com.plexapp.agents.imdb',
                             scanner='Plex Movie Scanner'))

    if opts.with_shows:
        tvshows_path = os.path.join(opts.destination, 'media', 'TV-Shows')
        makedirs(os.path.join(tvshows_path, 'Game of Thrones'), exist_ok=True)
        makedirs(os.path.join(tvshows_path, 'The 100'), exist_ok=True)

        required_tv_shows = {
            'Game of Thrones': [
                list(range(1, 11)),
                list(range(1, 11)),
            ],
            'The 100': [
                list(range(1, 14)),
                list(range(1, 17)),
            ]
        }

        for show_name, seasons in required_tv_shows.items():
            for season_id, episodes in enumerate(seasons, start=1):
                for episode_id in episodes:
                    expected_media_count += 1
                    episode_path = get_tvshow_path(show_name, season_id, episode_id)
                    if not os.path.isfile(episode_path):
                        cp(get_movie_path('Sintel', 2010), episode_path)

        print('Finished with TV Shows...')
        sections.append(dict(name='TV Shows', type='show', location='/data/TV-Shows', agent='com.plexapp.agents.thetvdb',
                             scanner='Plex Series Scanner'))

    if opts.with_music:
        music_path = os.path.join(opts.destination, 'media', 'Music')
        makedirs(music_path, exist_ok=True)

        artist_dst = os.path.join(music_path, 'Infinite State')
        dest_path = os.path.join(artist_dst, 'Unmastered Impulses')
        if not os.path.isdir(dest_path):
            zip_path = os.path.join(artist_dst, 'Unmastered Impulses.zip')
            if os.path.isfile(zip_path):
                import zipfile
                with zipfile.ZipFile(zip_path, 'r') as handle:
                    handle.extractall(artist_dst)
            else:
                download('https://github.com/kennethreitz/unmastered-impulses/archive/master.zip', '',
                         filename='Unmastered Impulses.zip', savepath=artist_dst, unpack=True, showstatus=True)
            os.rename(os.path.join(artist_dst, 'unmastered-impulses-master', 'mp3'), dest_path)
            rmtree(os.path.join(artist_dst, 'unmastered-impulses-master'))

        expected_media_count += len(glob(os.path.join(dest_path, '*.mp3')))

        artist_dst = os.path.join(music_path, 'Broke For Free')
        dest_path = os.path.join(artist_dst, 'Layers')
        if not os.path.isdir(dest_path):
            zip_path = os.path.join(artist_dst, 'Layers.zip')
            if not os.path.isfile(zip_path):
                download('https://freemusicarchive.org/music/zip/2803d3e9c9510c17d180b821b43b248e9db83487', '',
                         filename='Layers.zip', savepath=artist_dst, showstatus=True)
            makedirs(dest_path, exist_ok=True)
            import zipfile
            with zipfile.ZipFile(zip_path, 'r') as handle:
                handle.extractall(dest_path)

        expected_media_count += len(glob(os.path.join(dest_path, '*.mp3')))

        print('Finished with Music...')
        sections.append(dict(name='Music', type='artist', location='/data/Music', agent='com.plexapp.agents.none',
                             scanner='Plex Music Scanner'))

    if opts.with_photos:
        photos_path = os.path.join(opts.destination, 'media', 'Photos')
        makedirs(photos_path, exist_ok=True)

        has_photos = len(glob(os.path.join(photos_path, '*.jpg')))
        while has_photos < 10:
            has_photos += 1
            download('https://picsum.photos/800/600/?random', '',
                     filename='photo%d.jpg' % has_photos, savepath=photos_path)

        print('Photos collected, but we need to create an album later...')
        sections.append(dict(name='Photos', type='photo', location='/data/Photos', agent='com.plexapp.agents.none',
                             scanner='Plex Photo Scanner'))

    if sections:
        print('Ok, got the media, it`s time to create a library for you!')

        library = server.library

        finished = expected_media_count == 0

        processed_media = 0

        def alert_callback(data):
            global finished, processed_media
            if data['type'] == 'timeline':
                for entry in data['TimelineEntry']:
                    if entry['identifier'] == 'com.plexapp.plugins.library' and entry['state'] == 5 \
                            and entry['type'] in (SEARCHTYPES['movie'], SEARCHTYPES['episode'], SEARCHTYPES['track'],
                                                  SEARCHTYPES['photo']):
                        processed_media += 1

            if processed_media == expected_media_count:
                finished = True

        notifier = server.startAlertListener(alert_callback)

        first_section = sections.pop(0)

        # I don't know how to determinate of plex successfully started, so let's do it in creepy way
        success = False
        start_time = time()
        while not success and (time() - start_time < opts.bootstrap_timeout):
            try:
                library.add(**first_section)
                success = True
            except BadRequest as e:
                if 'the server is still starting up. Please retry later' in str(e):
                    sleep(1)
                else:
                    raise

        if not success:
            print('Something went wrong :(')
            exit(1)

        for section in sections:
            library.add(**section)

        print('Sections created, almost done! Please wait while metadata will be collected, it may take a couple '
              'minutes...')

        album_created = False
        start_time = time()
        while not finished and not (album_created and opts.with_photos and opts.with_photo_album):
            if time() - start_time >= opts.bootstrap_timeout:
                print('Metadata scan takes too long, probably something went really wrong')
                exit(1)
            if not album_created and opts.with_photos and opts.with_photo_album:
                photos = library.section('Photos').all()
                if len(photos) == has_photos:
                    server.createPlaylist('photo_album1', photos)
                    album_created = True
                    print('Photo album created')
            sleep(3)

    print('Base URL is %s' % server.url('', False))
    print('Auth token is %s' % account.authenticationToken)

    print('Server %s is ready to use!' % opts.server_name)
