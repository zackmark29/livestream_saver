from os import sep, makedirs, getcwd
from sys import platform
from sys import argv
import argparse
import logging
from pathlib import Path
from configparser import ConfigParser, ExtendedInterpolation
import traceback
import re
from shlex import split
from typing import Optional, Any, List, Dict
from livestream_saver import extract, util
import livestream_saver
from livestream_saver.monitor import YoutubeChannel, wait_block
from livestream_saver.download import YoutubeLiveStream
from livestream_saver.merge import merge, get_metadata_info
from livestream_saver.util import get_channel_id
from livestream_saver.request import YoutubeUrllibSession
from livestream_saver.smtp import NotificationHandler
from livestream_saver.hooks import HookCommand

logger = logging.getLogger('livestream_saver')
logger.setLevel(logging.DEBUG)

notif_h = NotificationHandler()

def parse_args(config) -> Dict[str, Any]:
    """
    Return a dict view of the argparse.Namespace.
    """
    parent_parser = argparse.ArgumentParser(
        description='Monitor a Youtube channel for any active live stream and \
record live streams from the first segment.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        argument_default=argparse.SUPPRESS,
        add_help=False
    )

    # Flags which are common to all sub-commands here
    log_levels = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
    parent_parser.add_argument('--log-level', action='store', type=str.upper,
        default=argparse.SUPPRESS,
        choices=log_levels,
        help=f'Log level. (Default: {config.get("DEFAULT", "log_level")})'
    )
    parent_parser.add_argument('-c', '--config-file',
        action='store', type=str,
        default=argparse.SUPPRESS,
        help='Path to config file to use.'\
             f' (Default: {config.get("DEFAULT", "config_file")})'
    )

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(
        dest='sub-command',
        help='Required sub-command.',
        required=True
    )

    # Sub-command "monitor"
    monitor_parser = subparsers.add_parser('monitor',
        help='Monitor a given Youtube channel for activity.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        parents=[parent_parser]
    )
    monitor_parser.set_defaults(func=monitor_mode)
    monitor_parser.add_argument('URL',
        nargs="?",
        default=None, type=str, metavar="YOUTUBE_CHANNEL_URL",
        help='The Youtube channel to monitor for live streams. \
Either a full youtube URL, /channel/ID, or /c/name format.'
    )
    monitor_parser.add_argument('--cookie',
        action='store', type=str, metavar="COOKIE_PATH",
        default=argparse.SUPPRESS,
        help='Path to Netscape formatted cookie file.'
    )
    monitor_parser.add_argument('-q', '--max-video-quality', action='store',
        default=argparse.SUPPRESS, type=int,
        help='Use best available video resolution up to this height in pixels.'\
             ' Example: "360" for maximum height 360p. Get the highest available'
             ' resolution by default.'
    )
    monitor_parser.add_argument('-o', '--output-dir', action='store',
        default=argparse.SUPPRESS, type=str,
        help='Output directory where to save channel data.'\
            f' (Default: {config.get("monitor", "output_dir")})'
    )
    monitor_parser.add_argument('--channel-name', action='store',
        default=argparse.SUPPRESS, type=str,
        help='User-defined name of the channel to monitor. Will fallback to \
channel ID deduced from the URL otherwise.'
    )
    monitor_parser.add_argument('-s', '--section', action='store',
        default=None, type=str,
        help=(
            'Override values from the section [monitor NAME] found in config file.'
            ' If none is specified, will load the first section in config with that name pattern.'
        )
    )

    monitor_group = monitor_parser.add_mutually_exclusive_group()
    monitor_parser.add_argument('-d', '--delete-source',
        action='store_true',
        help='Delete source segment files once the final \
merging of them has been done.'
    )
    monitor_group.add_argument('-n', '--no-merge',
        action='store_true',
        help='Do not merge segments after live streams has ended.'
    )
    monitor_parser.add_argument('-k', '--keep-concat',
        action='store_true',
        help='Keep concatenated intermediary files even if \
merging of streams has been successful. Only useful for troubleshooting.'
    )
    # monitor_parser.add_argument('--interactive', action='store_true',
    #    help='Allow user input to skip the current download.')
    monitor_parser.add_argument('--scan-delay',
        action='store', type=float,
        default=argparse.SUPPRESS,
        help='Interval in minutes to scan for channel activity.'\
             f' (Default: {config.getfloat("monitor", "scan_delay")})'
    )
    monitor_parser.add_argument('--email-notifications',
        action='store_true',
        default=argparse.SUPPRESS,
        help='Enables sending e-mail reports to administrator.'\
            f' (Default: {config.getboolean("monitor", "email_notifications")})'
    )
    monitor_parser.add_argument('--skip-download',
        action='store_true',
        default=argparse.SUPPRESS,
        help='Skip the download phase (useful to run hook scripts instead).'\
            f' (Default: {config.getboolean("monitor", "skip_download")})'
    )

    # Sub-command "download"
    download_parser = subparsers.add_parser('download',
        help='Download a given live stream by URL.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        parents=[parent_parser]
    )
    download_parser.set_defaults(func=download_mode)
    download_parser.add_argument('URL',
        type=str, metavar="YOUTUBE_VIDEO_URL",
        help='Youtube video stream URL to download.'
    )
    download_parser.add_argument('--cookie',
        action='store', type=str, metavar="COOKIE_PATH",
        default=argparse.SUPPRESS,
        help='Path to Netscape formatted cookie file.'
    )
    download_parser.add_argument('-q', '--max-video-quality',
        action='store', type=int,
        default=argparse.SUPPRESS,
        help='Use best available video resolution up to this height in pixels.'\
             ' Example: "360" for maximum height 360p. Get the highest available'
             ' resolution by default.'
    )
    download_parser.add_argument('-o', '--output-dir',
        action='store', type=str,
        default=argparse.SUPPRESS,
        help='Output directory where to write downloaded chunks.'\
              f' (Default: {config.get("download", "output_dir")})'
    )
    download_group = download_parser.add_mutually_exclusive_group()
    download_group.add_argument('-d', '--delete-source',
        action='store_true',
        help='Delete source files once final merge has been done.'
    )
    download_group.add_argument('-n', '--no-merge',
        action='store_true',
        help='Do not merge segments after live streams has ended.'
    )
    download_parser.add_argument('-k', '--keep-concat',
        action='store_true',
        help='Keep concatenated intermediary files even if merging of \
streams has been successful. Only useful for troubleshooting.'
    )
    download_parser.add_argument('--scan-delay',
        action='store', type=float,
        default=argparse.SUPPRESS,
        help='Interval in minutes to scan for status update.'\
             f' (Default: {config.getfloat("download", "scan_delay")})'
    )
    download_parser.add_argument('--email-notifications',
        action='store_true',
        default=argparse.SUPPRESS,
        help='Enable sending e-mail reports to administrator.'\
            f' (Default: {config.getboolean("download", "email_notifications")})'
    )
    download_parser.add_argument('--skip-download',
        action='store_true',
        default=argparse.SUPPRESS,
        help='Skip the download phase (useful to run hook scripts instead).'\
            f' (Default: {config.getboolean("download", "skip_download")})'
    )

    # Sub-command "merge"
    merge_parser = subparsers.add_parser('merge',
        help='Merge downloaded segments into one single video file.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        parents=[parent_parser]
    )
    merge_parser.set_defaults(func=merge_mode)
    merge_parser.add_argument('PATH',
        type=str,
        help='Path to directory holding vid/aud sub-directories \
in which segments have been downloaded as well as the metadata.txt file.'
    )
    merge_parser.add_argument('-d', '--delete-source',
        action='store_true',
        help='Delete source files (vid/aud) once final merging of \
streams has been successfully done.'
    )
    merge_parser.add_argument('-k', '--keep-concat',
        action='store_true',
        help='Keep concatenated intermediary files even if merging of \
streams has been successful. This is only useful for debugging.'
    )
    merge_parser.add_argument('-o', '--output-dir',
        action='store', type=str,
        default=None,
        help='Output directory where to write final merged file.'
    )

    # Sub-command "test-notification"
    monitor_parser = subparsers.add_parser('test-notification',
        help='Send a test e-mail to check if your configuration works.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        parents=[parent_parser]
    )

    return vars(parser.parse_args())


# Base name for each "event"
event_props = [
    "on_upcoming_detected",
    "on_video_detected",
    "on_download_initiated",
    "on_download_started",
    "on_download_ended",
    "on_merge_done",
]


def _get_hook_from_config(
    config: ConfigParser, section: str, hook_name: str) -> Optional[HookCommand]:
    """Generate a HookCommand if one has been requested in a section."""
    # Remember this will try to get from the DEFAULT section if not found
    # and THEN use the fallback value.
    cmd = config.getlist(section, hook_name, fallback=None)
    if not cmd:
        return
    if not config.getboolean(section, hook_name + "_enabled", fallback=False):
        return
    logged = config.getboolean(section, hook_name + "_logged", fallback=False)

    allow_regex = None
    if allow_regex_str := config.get(
    section, hook_name + "_allow_regex", fallback=None):
        allow_regex_str = allow_regex_str.strip("\"\'")
        allow_regex = re.compile(allow_regex_str)
        if not allow_regex.pattern:
            allow_regex = None

    block_regex = None
    if block_regex_str := config.get(
    section, hook_name + "_block_regex", fallback=None):
        block_regex_str = block_regex_str.strip("\"\'")
        block_regex = re.compile(block_regex_str)
        if not block_regex.pattern:
            block_regex = None


    hook = HookCommand(
        cmd=cmd,
        logged=logged,
        event_name=hook_name,
        allow_regex=allow_regex,
        block_regex=block_regex
    )

    # The same object is used on many different videos, others are only used
    # as part of the live stream object, so they are usually called once only anyway.
    if hook_name in ("on_upcoming_detected", "on_video_detected"):
        hook.call_only_once = False
    return hook


def get_hooks_for_section(section: str, config: ConfigParser) -> Dict:
    cmds = {}
    for hook_name in event_props:
        try:
            if hookcommand := _get_hook_from_config(config, section, hook_name):
                cmds[hook_name] = hookcommand
        except Exception as e:
            logger.critical(
                f"Error parsing hook command \"{hook_name}\" of section "
                f"{section}: {e}."
            )
    return cmds


class EmptyRegexException(Exception):
    """Pattern is empty."""
    pass


def _get_regex_from_config(
    section: str, config: ConfigParser, regex: str) -> Optional[re.Pattern]:
    if regex_str := config.get(section, regex, fallback=None):
        regex_str = regex_str.strip("\"\'")
        logger.debug(f"For \"{regex}\" found \"{regex_str}\" from section {section}")
        if regex_str in (r'""', r"''"):
            raise EmptyRegexException
        pattern = re.compile(regex_str, re.I|re.M)
        if not pattern.pattern:
            raise EmptyRegexException
        return pattern
    return None


def _get_target_params(
    config: ConfigParser,
    args: Dict,
    sub_cmd: str,
    override: Optional[str]) -> Dict:
    """
    Deal with case where URL positional argument is not supplied by the user,
    but could be supplied in config file in the appropriate section.
    """
    # This could be called from either Monitor or Download mode.
    # TODO We could compare the URL supplied as argument with the
    # [channel monitor] URL value and automatically assign the corresponding
    # scan_delay if it's not already present in the CLI args.

    params = {
        "URL": args.get("URL", None),
        "channel_name": args.get("channel_name", None),
        "scan_delay": config.getfloat(sub_cmd, "scan_delay", vars=args),
        "skip_download": config.getboolean(sub_cmd, "skip_download", vars=args),
        "hooks": get_hooks_for_section(sub_cmd, config),
        "filters": {
            "allow_regex": None,
            "block_regex": None
        }
    }

    # This may throw, it should crash the program to avoid bad surprises
    if sub_cmd == "monitor":
        for regex_str in ("allow_regex", "block_regex"):
            try:
                params["filters"][regex_str] = _get_regex_from_config(
                    sub_cmd, config, regex_str
                )
            except EmptyRegexException:
                logger.debug(f"Empty value in {sub_cmd} section for {regex_str}")
                pass

    # The user already has explicitly passed this argument, so we can ignore
    # any [monitor channel] section.
    if params.get("URL") is not None or sub_cmd != "monitor":
        return params

    # These sections should override default values from the "monitor" section.
    # Use the section specified on the CLI, otherwise use the first one we find.
    for section in config.sections():
        if not section.startswith("monitor"):
            continue
        # We need at least one space between monitor and the channel name
        if len(section) <= len("monitor "):
            continue
        if override is None or override == section[len("monitor "):]:
            logger.info(f"Using custom monitor section \"{section}\".")
            params["URL"] = config.get(
                section, "URL", fallback=None
            )
            params["channel_name"] = config.get(
                section, "channel_name", vars=args, fallback=None
            )
            # Use the value from monitor section if missing
            params["scan_delay"] = config.getfloat(
                section, "scan_delay", vars=args,
                fallback=params["scan_delay"]
            )
            params["skip_download"] = config.getboolean(
                section, "skip_download", vars=args,
                fallback=params["skip_download"]
            )

            # Update any hook already present with those defined in that section
            overriden_hooks = get_hooks_for_section(section, config)
            params["hooks"].update(overriden_hooks)

            # Update regex if it has been specified in the section
            for regex_str in ("allow_regex", "block_regex"):
                try:
                    if re := _get_regex_from_config(section, config, regex_str):
                        params["filters"][regex_str] = re
                except EmptyRegexException:
                    logger.debug(f"Empty value in {section} section for {regex_str}.")
                    # In case of empty string, remove any regex that could have
                    # been set from the monitor section.
                    params["filters"][regex_str] = None
            # Use the first section encountered if override is None
            break

    if params.get("URL") is None:
        raise Exception(
            "No URL specified, neither as a command-line argument nor in"
            " a [monitor {channel}] section of the config file."
            " Config file path was:"
            f" \"{config.get('DEFAULT', 'config_file', vars=args)}\""
        )
    return params


def monitor_mode(config, args):
    URL = args["URL"]
    channel_id = args["channel_id"]
    scan_delay = args["scan_delay"]

    session = YoutubeUrllibSession(
        config.get("monitor", "cookie", vars=args, fallback=None),
        notifier=notif_h
    )

    URL = util.sanitize_channel_url(URL)

    ch = YoutubeChannel(
        URL, channel_id, session,
        output_dir=args["output_dir"], hooks=args["hooks"])

    logger.info(f"Monitoring channel: {ch.id}")

    while True:
        live_videos = []
        try:
            ch.upcoming_videos  # get a list of upcoming videos
            live_videos = ch.filter_videos('isLiveNow')  # get the actual live stream
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Live videos found for channel "
                    f"\"{ch.get_channel_name()}\": "
                    f"{live_videos if len(live_videos) else None}"
                )
        except Exception as e:
            # Handle urllib.error.URLError <urlopen error [Errno -3] Temporary failure in name resolution>
            logger.exception(e)
            pass

        if len(live_videos) == 0:
            wait_block(min_minutes=scan_delay, variance=3.5)
            continue

        target_live = live_videos[0]
        _id = target_live.get('videoId')
        sub_output_dir = args["output_dir"] / f"stream_capture_{_id}"
        livestream = None
        try:
            livestream = YoutubeLiveStream(
                url=f"https://www.youtube.com{target_live.get('url')}", # /watch?v=...
                output_dir=sub_output_dir,
                session=ch.session,
                video_id=_id,
                max_video_quality=config.getint(
                    "monitor", "max_video_quality", vars=args, fallback=None
                ),
                hooks=args["hooks"],
                skip_download=args.get("skip_download", False),
                filters=args["filters"],
                log_level=config.get("monitor", "log_level", vars=args)
            )
        except ValueError as e:
            # Constructor may throw
            logger.critical(e)
            wait_block(min_minutes=scan_delay, variance=3.5)
            continue

        logger.info(
            f"Found live: {_id} title: {target_live.get('title')}. "
            "Downloading..."
        )

        try:
            livestream.download()
        except Exception as e:
            logger.exception(
                f"Got error in stream download but continuing...\n {e}"
            )
            pass

        if livestream.skip_download:
            notif_h.send_email(
                subject=(
                    f"Skipped download of {ch.get_channel_name()} - "
                    f"{livestream.title} {_id}"
                ),
                message_text=f"Hooks scheduled to run were: {args.get('hooks')}"
            )

        if livestream.done:
            logger.info(f"Finished downloading {_id}.")
            notif_h.send_email(
                subject=f"Finished downloading {ch.get_channel_name()} - \
{livestream.title} {_id}",
                message_text=f""
            )
            if not config.getboolean("monitor", "no_merge", vars=args):
                logger.info("Merging segments...")
                # TODO in a separate thread?
                merged = merge(
                    info=livestream.video_info,
                    data_dir=livestream.output_dir,
                    keep_concat=config.getboolean(
                        "monitor", "keep_concat", vars=args
                    ),
                    delete_source=config.getboolean(
                        "monitor", "delete_source", vars=args
                    )
                )
                if merged is not None:
                    livestream.on("on_merge_done")

                # TODO get the updated stream title from the channel page if
                # the stream was recorded correctly?
        if livestream.error:
            notif_h.send_email(
                subject=f"Error downloading stream {livestream.video_id}",
                message_text=f"Error was: {livestream.error}\n"
                              "Resuming monitoring..."
            )
            logger.critical("Error during stream download! Resuming monitoring...")
            pass

        wait_block(min_minutes=scan_delay, variance=3.5)
    return 1


def download_mode(config, args):
    session = YoutubeUrllibSession(
        config.get("download", "cookie", vars=args, fallback=None),
        notifier=notif_h
    )
    try:
        dl = YoutubeLiveStream(
            url=args.get("URL"),
            output_dir=args["output_dir"],
            session=session,
            video_id=args["video_id"],
            max_video_quality=config.getint(
                "download", "max_video_quality", vars=args, fallback=None
            ),
            hooks=args["hooks"],
            skip_download=config.getboolean(
                "download", "skip_download", vars=args, fallback=False
            ),
            # no filters in this mode, we assume the user knows what they're doing
            filters={},
            log_level=config.get("download", "log_level", vars=args)
        )
    except ValueError as e:
        logger.critical(e)
        return 1

    dl.download(config.getfloat("download", "scan_delay", vars=args))

    if dl.done and not config.getboolean("download", "no_merge", vars=args):
        logger.info("Merging segments...")
        merge(
            info=dl.video_info,
            data_dir=dl.output_dir,
            keep_concat=config.getboolean("download", "keep_concat", vars=args),
            delete_source=config.getboolean("download", "delete_source", vars=args)
        )
    return 0


def merge_mode(config, args):
    data_path = Path(args["PATH"]).resolve()
    info = get_metadata_info(data_path)
    written_file = merge(
        info=info,
        data_dir=data_path,
        output_dir=config.get("merge", "output_dir", vars=args),
        keep_concat=config.getboolean("merge", "keep_concat", vars=args),
        delete_source=config.getboolean("merge", "delete_source", vars=args)
    )

    if not written_file:
        logger.critical("Something failed. Please report the issue with logs.")
        return 1
    return 0


def log_enabled(config, args, mode_str):
    """Sanitize log level input value, return False if disabled by user."""
    if level := config.get(mode_str, "log_level", vars=args):
        # if level == "NONE":
        #     return False
        log_level = getattr(logging, level, None)
        if not isinstance(log_level, int):
            raise ValueError(f'Invalid log-level for {mode_str} mode: {level}')
    return True


def setup_logger(*, output_filepath, loglevel, log_to_file=True) -> logging.Logger:
    # This uses the global variable "logger"
    if loglevel is None:
        logger.disabled = True
        return logger

    if isinstance(loglevel, str):
        loglevel = str.upper(loglevel)

    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
    )

    if log_to_file:
        logfile = logging.FileHandler(
            filename=output_filepath, delay=True, encoding='utf-8'
        )
        # FIXME DEBUG by default for file
        logfile.setLevel(logging.DEBUG)
        logfile.setFormatter(formatter)
        logger.addHandler(logfile)

    # Console stdout handler
    conhandler = logging.StreamHandler()
    conhandler.setLevel(loglevel)
    conhandler.setFormatter(formatter)
    logger.addHandler(conhandler)
    return logger


def init_config() -> ConfigParser:
    """Get a ConfigParser with sane default values."""
    # Create user config directory if it doesn't already exist
    conf_filename = "livestream_saver.cfg"
    if platform == "Win32":
        config_dir = Path.home() / "livestream_saver.cfg"
    else:
        config_dir = Path.home() / ".config/livestream_saver"
        # config_dir.mkdir(exist_ok=True)

    CONFIG_DEFAULTS = {
        "config_dir": str(config_dir),
        "config_file": config_dir / conf_filename,
        "output_dir": getcwd(),
        "log_level": "INFO",

        "delete_source": "False",
        "keep_concat": "False",
        "no_merge": "False",
        "skip_download": "False",

        "email_notifications": "False",
        "smtp_server": "",
        "smtp_port": "",
        "to_email": "",
        "from_email": "",
    }

    def parse_as_list(data: str) -> Optional[List[str]]:
        """Split a string into a valid shell command as a list of string."""
        # if not data:
        #     return None
        return split(data)

    # We cannot use the basic interpolation or it would conflict with the
    # output filename template syntax from yt-dlp. "None" works well but
    # we would lose the ability to use variables.
    config = ConfigParser(
        CONFIG_DEFAULTS,
        interpolation=ExtendedInterpolation(),
        converters={'list': parse_as_list}
    )
    # Set defaults for each section
    config.add_section("monitor")
    config.set("monitor", "scan_delay", "15.0")  # minutes
    config.add_section("download")
    config.set("download", "scan_delay", "2.0")  # minutes
    config.add_section("merge")
    config.add_section("test-notification")
    return config


def update_config(config, args) -> None:
    """Load the configuration file specified as argument into config object.
    If none is specified, load the configuration file located in the current
    working directory if possible."""
    conf_file = args.get("config_file")
    if conf_file:
        if not Path(conf_file).is_file():
            logging.critical(
                f"Config file \"{conf_file}\" is not a valid file. "
                "Continuing with default values only!")
            return
        read_conf_files = config.read(conf_file)
        if not read_conf_files:
            logging.critical(f"Failed to read config file from {conf_file}")
        return
    # No config file specified, get it from ~/.config by default
    conf_file = Path(config.get("DEFAULT", "config_file"))
    if not conf_file.exists():
        # try in the current working directory just in case
        conf_file = Path.cwd() / "livestream_saver.cfg"
        if not conf_file.exists():
            return
    read_conf_files = config.read(conf_file)
    if not read_conf_files:
        logging.critical(f"Failed to read config file from {conf_file}.")


def main():
    config: ConfigParser = init_config()
    args = parse_args(config)
    update_config(config, args)
    # print(f"parse_config() -> {[opt for sect in config.sections() for opt in config.options(sect)]}")

    notif_h.setup(config, args)

    sub_cmd = args.get("sub-command")
    if sub_cmd is None:
        print("No sub-command used. Exiting.")
        return

    global logger
    log_enabled(config, args, sub_cmd)

    logfile_path = Path("")  # cwd by default
    if sub_cmd == "monitor":
        params = _get_target_params(config, args, sub_cmd=sub_cmd, override=args["section"])
        args["URL"] = params.get("URL")
        channel_name = params.get("channel_name")
        args["channel_name"] = channel_name
        args["scan_delay"] = params.get("scan_delay")
        args["hooks"] = params.get("hooks")
        args["skip_download"] = params.get("skip_download")
        args["filters"] = params.get("filters", {})

        channel_id = get_channel_id(args["URL"], service_name="youtube")
        args["channel_id"] = channel_id

        # We need to setup output dir before instanciating downloads
        # because we use it to store our logs
        output_dir = Path(config.get("monitor", "output_dir", vars=args))
        if channel_name is not None:
            output_dir = output_dir / channel_name
        else:
            output_dir = output_dir / channel_id

        makedirs(output_dir, exist_ok=True)
        args["output_dir"] = output_dir

        logfile_path = output_dir / f'monitor_{channel_id}.log'

        logger = setup_logger(
            output_filepath=logfile_path,
            loglevel=config.get(sub_cmd, "log_level", vars=args)
        )
        logger.debug(f"Regex filters {[f'{k}: {v}' for k, v in args['filters'].items()]}")
        args["logger"] = logger
    elif sub_cmd  == "download":
        output_dir = Path(
            config.get(
                "download", "output_dir", vars=args, fallback=getcwd()
            )
        )
        URL = args.get("URL", "")  # pass empty string for get_video_id()
        args["hooks"] = get_hooks_for_section(sub_cmd, config)

        video_id = extract.get_video_id(url=URL)
        args["video_id"] = video_id
        output_dir = util.create_output_dir(
            output_dir=output_dir, video_id=video_id
        )
        args["output_dir"] = output_dir

        logfile_path = output_dir / "download.log"
        logger = setup_logger(
            output_filepath=logfile_path,
            loglevel=config.get(sub_cmd, "log_level", vars=args)
        )
        args["logger"] = logger
    elif sub_cmd == "merge":
        logfile_path = Path(args["PATH"]) / "merge.log"
        setup_logger(
            output_filepath= logfile_path,
            loglevel=config.get(sub_cmd, "log_level", vars=args)
        )
    elif sub_cmd == "test-notification":
        logfile_path = getcwd()
        setup_logger(
            output_filepath=logfile_path,
            loglevel=config.get(sub_cmd, "log_level", vars=args),
            log_to_file=False
        )
        if notif_h.disabled:
            print("Emails are currenly disabled by configuration.")
            return
        notif_h.send_email(
            subject=f"{__file__.split(sep)[-1]} test email.",
            message_text="The current configuration works fine!\nYay.\n"
                         "You will receive notifications if the program "
                         "encounters an issue while doing its tasks."
        )
        print(
            f"Sent test e-mail to {notif_h.receiver_email} via "
            f"SMTP server {notif_h.smtp_server}:{notif_h.smtp_port}... "
            "Check your inbox!"
            )
        if not notif_h.disabled:
            notif_h.q.join()
        return
    else:
        print("Wrong sub-command. Exiting.")
        return

    error = 0
    try:
        error = args["func"](config, args)
    except Exception as e:
        error = 1
        from sys import exc_info
        exc_type, exc_value, exc_traceback = exc_info()
        logger.exception(e)
        notif_h.send_email(
            subject=f"{argv[0].split(sep)[-1]} crashed!",
            message_text=f"Mode: {args.get('sub-command', '').split('_')[0]}\n" \
            + f"Exception was: {e}\n" \
            + "\n".join(
                traceback.format_exception(
                    exc_type, exc_value,exc_traceback
                )
            ),
            attachments=[logfile_path]
        )

    # We need to join here (or sleep long enough) otherwise any email still
    # in the queue will fail to get sent because we exited too soon!
    if not notif_h.disabled:
        notif_h.q.join()
        # notif_h.thread.join()
    logging.shutdown()
    return error


if __name__ == "__main__":
    exit(main())
