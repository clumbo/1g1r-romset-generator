import getopt
import hashlib
import os
import re
import shutil
import sys
from concurrent.futures.thread import ThreadPoolExecutor
from io import BufferedIOBase
from typing import Optional, Match, List, Dict, Pattern, Callable, Union, \
    BinaryIO
from zipfile import ZipFile, BadZipFile

import datafile
from classes import GameEntry, Score, RegionData, ProgressBar, \
    GameEntryHelper, GameEntryKeyGenerator, FileData, FileDataUtils
from utils import get_index, check_in_pattern_list, to_int_list, add_padding, \
    get_or_default, available_columns, trim_to, is_valid

QUIT = False
PROGRESSBAR: Optional[ProgressBar] = None

FOUND_PREFIX = 'Found: '

THREADS: Optional[int] = None

CHUNK_SIZE = 33554432  # 32 MB

FILE_PREFIX = 'file:'

UNSELECTED = 10000
NOT_PRERELEASE = "Z"

AVOIDED_ROM_BASE = 1000

NO_WARNING: bool = False

COUNTRY_REGION_CORRELATION = [
    # Language needs checking
    RegionData('ASI', re.compile(r'(Asia)', re.IGNORECASE), ['zh']),
    RegionData('ARG', re.compile(r'(Argentina)', re.IGNORECASE), ['es']),
    RegionData('AUS', re.compile(r'(Australia)', re.IGNORECASE), ['en']),
    RegionData('BRA', re.compile(r'(Brazil)', re.IGNORECASE), ['pt']),
    # Language needs checking
    RegionData('CAN', re.compile(r'(Canada)', re.IGNORECASE), ['en', 'fr']),
    RegionData(
        'CHN',
        re.compile(r'((China)|(Hong Kong))', re.IGNORECASE),
        ['zh']),
    RegionData('DAN', re.compile(r'(Denmark)', re.IGNORECASE), ['da']),
    RegionData('EUR', re.compile(r'((Europe)|(World))', re.IGNORECASE), ['en']),
    RegionData('FRA', re.compile(r'(France)', re.IGNORECASE), ['fr']),
    RegionData('FYN', re.compile(r'(Finland)', re.IGNORECASE), ['fi']),
    RegionData('GER', re.compile(r'(Germany)', re.IGNORECASE), ['de']),
    RegionData('GRE', re.compile(r'(Greece)', re.IGNORECASE), ['el']),
    RegionData('ITA', re.compile(r'(Italy)', re.IGNORECASE), ['it']),
    RegionData('JPN', re.compile(r'((Japan)|(World))', re.IGNORECASE), ['ja']),
    RegionData('HOL', re.compile(r'(Netherlands)', re.IGNORECASE), ['nl']),
    RegionData('KOR', re.compile(r'(Korea)', re.IGNORECASE), ['ko']),
    RegionData('MEX', re.compile(r'(Mexico)', re.IGNORECASE), ['es']),
    RegionData('NOR', re.compile(r'(Norway)', re.IGNORECASE), ['no']),
    RegionData('RUS', re.compile(r'(Russia)', re.IGNORECASE), ['ru']),
    RegionData('SPA', re.compile(r'(Spain)', re.IGNORECASE), ['es']),
    RegionData('SWE', re.compile(r'(Sweden)', re.IGNORECASE), ['sv']),
    RegionData('USA', re.compile(r'((USA)|(World))', re.IGNORECASE), ['en']),
    # Language needs checking
    RegionData('TAI', re.compile(r'(Taiwan)', re.IGNORECASE), ['zh'])
]

sections_regex = re.compile(r'\(([^()]+)\)')
bios_regex = re.compile(re.escape('[BIOS]'), re.IGNORECASE)
program_regex = re.compile(r'\((?:Test\s*)?Program\)', re.IGNORECASE)
enhancement_chip_regex = re.compile(r'\(Enhancement\s*Chip\)', re.IGNORECASE)
unl_regex = re.compile(re.escape('(Unl)'), re.IGNORECASE)
pirate_regex = re.compile(re.escape('(Pirate)'), re.IGNORECASE)
promo_regex = re.compile(re.escape('(Promo)'), re.IGNORECASE)
beta_regex = re.compile(r'\(Beta(?:\s*([a-z0-9.]+))?\)', re.IGNORECASE)
proto_regex = re.compile(r'\(Proto(?:\s*([a-z0-9.]+))?\)', re.IGNORECASE)
sample_regex = re.compile(r'\(Sample(?:\s*([a-z0-9.]+))?\)', re.IGNORECASE)
demo_regex = re.compile(r'\(Demo(?:\s*([a-z0-9.]+))?\)', re.IGNORECASE)
rev_regex = re.compile(r'\(Rev\s*([a-z0-9.]+)\)', re.IGNORECASE)
version_regex = re.compile(r'\(v\s*([a-z0-9.]+)\)', re.IGNORECASE)
languages_regex = re.compile(r'\(([a-z]{2}(?:[,+][a-z]{2})*)\)', re.IGNORECASE)
bad_regex = re.compile(re.escape('[b]'), re.IGNORECASE)
zip_regex = re.compile(re.escape(os.path.extsep) + r'zip$', re.IGNORECASE)


def parse_revision(name: str) -> str:
    return get_or_default(rev_regex.search(name), '0')


def parse_version(name: str) -> str:
    return get_or_default(version_regex.search(name), '0')


def parse_prerelease(match: Optional[Match]) -> str:
    return get_or_default(match, NOT_PRERELEASE)


def parse_region_data(name: str) -> List[RegionData]:
    parsed = []
    for section in sections_regex.finditer(name):
        elements = [element.strip() for element in section.group(1).split(',')]
        for element in elements:
            for region_data in COUNTRY_REGION_CORRELATION:
                if region_data.pattern \
                        and region_data.pattern.fullmatch(element):
                    parsed.append(region_data)
    return parsed


def parse_languages(name: str) -> List[str]:
    lang_matcher = languages_regex.search(name)
    languages = []
    if lang_matcher:
        for entry in lang_matcher.group(1).split(','):
            for lang in entry.split('+'):
                languages.append(lang.lower())
    return languages


def get_region_data(code: str) -> Optional[RegionData]:
    code = code.upper() if code else code
    region_data = None
    for r in COUNTRY_REGION_CORRELATION:
        if r.code == code:
            region_data = r
            break
    if not region_data:
        # We don't know which region this is, but we should filter/classify it
        if not NO_WARNING:
            print(
                'WARNING: unrecognized region (%s)' % code,
                file=sys.stderr)
        region_data = RegionData(code, None, [])
        COUNTRY_REGION_CORRELATION.append(region_data)
    return region_data


def get_languages(region_data_list: List[RegionData]) -> List[str]:
    languages = []
    for region_data in region_data_list:
        for language in region_data.languages:
            if language not in languages:
                languages.append(language)
    return languages


def is_present(code: str, region_data: List[RegionData]) -> bool:
    for r in region_data:
        if r.code == code:
            return True
    return False


def validate_dat(file: str, use_hashes: bool) -> None:
    root = datafile.parse(file, silence=True)
    has_cloneof = False
    lacks_sha1 = False
    offending_entry = ''
    for game in root.game:
        if game.cloneof:
            has_cloneof = True
            break
    for game in root.game:
        for game_rom in game.rom:
            if not game_rom.sha1:
                lacks_sha1 = True
                offending_entry = game.name
                break
    if use_hashes and lacks_sha1:
        print(
            'ERROR: Cannot use hash information because DAT lacks SHA1 digests '
            'for [%s].' % offending_entry)
        sys.exit(1)
    if not has_cloneof:
        print('This DAT *seems* to be a Standard DAT')
        print('A Parent/Clone XML DAT is required to generate a 1G1R ROM set')
        if use_hashes:
            print(
                'If you are using this to rename files based on their hashes, '
                'a Standard DAT is enough')
        answer = input('Do you want to continue anyway? (y/n)\n')
        if answer.strip() not in ('y', 'Y'):
            sys.exit()


def parse_games(
        file: str,
        filter_bios: bool,
        filter_program: bool,
        filter_enhancement_chip: bool,
        filter_pirate: bool,
        filter_promo: bool,
        filter_unlicensed: bool,
        filter_proto: bool,
        filter_beta: bool,
        filter_demo: bool,
        filter_sample: bool,
        exclude: List[Pattern]) -> Dict[str, List[GameEntry]]:
    games = {}
    root = datafile.parse(file, silence=True)
    for input_index in range(0, len(root.game)):
        game = root.game[input_index]
        beta_match = beta_regex.search(game.name)
        demo_match = demo_regex.search(game.name)
        sample_match = sample_regex.search(game.name)
        proto_match = proto_regex.search(game.name)
        if filter_bios and bios_regex.search(game.name):
            continue
        if filter_unlicensed and unl_regex.search(game.name):
            continue
        if filter_pirate and pirate_regex.search(game.name):
            continue
        if filter_promo and promo_regex.search(game.name):
            continue
        if filter_program and program_regex.search(game.name):
            continue
        if filter_enhancement_chip and enhancement_chip_regex.search(game.name):
            continue
        if filter_beta and beta_match:
            continue
        if filter_demo and demo_match:
            continue
        if filter_sample and sample_match:
            continue
        if filter_proto and proto_match:
            continue
        if check_in_pattern_list(game.name, exclude):
            continue
        is_parent = not game.cloneof
        is_bad = bool(bad_regex.search(game.name))
        beta = parse_prerelease(beta_match)
        demo = parse_prerelease(demo_match)
        sample = parse_prerelease(sample_match)
        proto = parse_prerelease(proto_match)
        is_prerelease = bool(
            beta_match
            or demo_match
            or sample_match
            or proto_match)
        revision = parse_revision(game.name)
        version = parse_version(game.name)
        region_data = parse_region_data(game.name)
        for release in game.release:
            if release.region and not is_present(release.region, region_data):
                region_data.append(get_region_data(release.region))
        languages = parse_languages(game.name)
        if not languages:
            languages = get_languages(region_data)
        parent_name = game.cloneof if game.cloneof else game.name
        region_codes = [rd.code for rd in region_data]
        game_entries: List[GameEntry] = []
        for region in region_codes:
            game_entries.append(
                GameEntry(
                    is_bad,
                    is_prerelease,
                    region,
                    languages,
                    input_index,
                    revision,
                    version,
                    sample,
                    demo,
                    beta,
                    proto,
                    is_parent,
                    game.name,
                    game.rom if game.rom else []))
        if game_entries:
            if parent_name not in games:
                games[parent_name] = game_entries
            else:
                games[parent_name].extend(game_entries)
        elif not NO_WARNING:
            print(
                'WARNING [%s]: no recognizable regions found' % game.name,
                file=sys.stderr)
        if not game.rom and not NO_WARNING:
            print(
                'WARNING [%s]: no ROMs found in the DAT file' % game.name,
                file=sys.stderr)
    return games


def pad_values(
        games: List[GameEntry],
        get_function: Callable[[GameEntry], str],
        set_function: Callable[[GameEntry, str], None]) -> None:
    padded = add_padding([get_function(g) for g in games])
    for i in range(0, len(padded)):
        set_function(games[i], padded[i])


def language_value(
        languages: List[str],
        weight: int,
        selected_languages: List[str]) -> int:
    return sum([
        (get_index(selected_languages, lang, -1) + 1) * weight * -1
        for lang in languages])


def index_files(
        input_dir: str,
        dat_file: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    root = datafile.parse(dat_file, silence=True)
    for game in root.game:
        for rom_entry in game.rom:
            result[rom_entry.sha1.lower()] = ""
    print('Scanning directory: %s\033[K' % input_dir, file=sys.stderr)
    files_data = []
    for root, dirs, files in os.walk(input_dir):
        for file in files:
            full_path = os.path.join(root, file)
            try:
                print(
                    '%s%s\033[K' % (
                        FOUND_PREFIX,
                        trim_to(
                            full_path,
                            available_columns(FOUND_PREFIX) - 2)),
                    end='\r',
                    file=sys.stderr)
                file_size = os.path.getsize(full_path)
                files_data.append(FileData(file_size, full_path))
            except OSError as e:
                print(
                    'Error while reading file: %s\033[K' % e,
                    file=sys.stderr)
    files_data.sort(key=FileDataUtils.get_size, reverse=True)
    print('%s%i files\033[K' % (FOUND_PREFIX, len(files_data)), file=sys.stderr)

    if files_data:
        global PROGRESSBAR
        PROGRESSBAR = ProgressBar(len(files_data), prefix='Calculating hashes')
        PROGRESSBAR.print(0)

        def process_with_progress(file_data: FileData) -> Dict[str, str]:
            if QUIT:
                return {}
            this_result = process_file(file_data)
            if QUIT:
                return {}
            PROGRESSBAR.print()
            return this_result

        with ThreadPoolExecutor(THREADS) as pool:
            intermediate_results = pool.map(process_with_progress, files_data)
        for intermediate_result in intermediate_results:
            for key, value in intermediate_result.items():
                if key in result and not is_zip(result[key]):
                    result[key] = value
    return result


def process_file(file_data: FileData) -> Dict[str, str]:
    full_path = file_data.path
    result: Dict[str, str] = {}
    if is_zip(full_path):
        try:
            with ZipFile(full_path) as compressed_file:
                for name in compressed_file.namelist():
                    if not QUIT:
                        with compressed_file.open(name) as internal_file:
                            digest = compute_hash(internal_file)
                            result[digest] = full_path
        except BadZipFile as e:
            print(
                'Error while reading file [%s]: %s\033[K' % (full_path, e),
                file=sys.stderr)
    try:
        with open(full_path, 'rb') as uncompressed_file:
            digest = compute_hash(uncompressed_file)
            if digest not in result or is_zip(result[digest]):
                result[digest] = full_path
    except IOError as e:
        print(
            'Error while reading file: %s\033[K' % e,
            file=sys.stderr)
    return result


def compute_hash(
        internal_file: Union[BufferedIOBase, BinaryIO]) -> str:
    hasher = hashlib.sha1()
    while not QUIT:
        chunk = internal_file.read(CHUNK_SIZE)
        if not chunk and not QUIT:
            break
        hasher.update(chunk)
    return hasher.hexdigest().lower()


def is_zip(file: str) -> bool:
    return bool(zip_regex.search(file))


def main(argv: List[str]):
    global NO_WARNING
    try:
        opts, args = getopt.getopt(argv, 'hd:r:e:i:vo:l:w:', [
            'help',
            'dat=',
            'regions=',
            'no-bios',
            'no-program',
            'no-enhancement-chip',
            'no-beta',
            'no-demo',
            'no-sample',
            'no-proto',
            'no-pirate',
            'no-promo',
            'no-all',
            'no-unlicensed',
            'all-regions',
            'early-revisions',
            'early-versions',
            'input-order',
            'extension=',
            'use-hashes',
            'input-dir=',
            'prefer=',
            'avoid=',
            'exclude=',
            'exclude-after=',
            'separator=',
            'ignore-case',
            'regex',
            'verbose',
            'output-dir=',
            'languages=',
            'prioritize-languages',
            'language-weight=',
            'prefer-parents',
            'prefer-prereleases',
            'all-regions-with-lang',
            'debug',
            'move',
            'no-warning',
            'chunk-size=',
            'threads='
        ])
    except getopt.GetoptError as e:
        print(e, file=sys.stderr)
        print_help()
        sys.exit(1)

    dat_file = ""
    filter_bios = False
    filter_program = False
    filter_enhancement_chip = False
    filter_unlicensed = False
    filter_pirate = False
    filter_promo = False
    filter_proto = False
    filter_beta = False
    filter_demo = False
    filter_sample = False
    all_regions = False
    all_regions_with_lang = False
    revision_asc = False
    version_asc = False
    verbose = False
    use_hashes = False
    input_order = False
    selected_regions: List[str] = []
    file_extension = ""
    input_dir = ""
    prefer_str = ""
    exclude_str = ""
    avoid_str = ""
    exclude_after_str = ""
    sep = ','
    ignore_case = False
    regex = False
    output_dir = ""
    selected_languages: List[str] = []
    prioritize_languages = False
    prefer_parents = False
    prefer_prereleases = False
    language_weight = 3
    debug = False
    move = False
    for opt, arg in opts:
        if opt in ('-h', '--help'):
            print_help()
            sys.exit()
        if opt in ('-r', '--regions'):
            selected_regions = [x.strip().upper() for x in arg.split(',')
                                if is_valid(x)]
        if opt in ('-l', '--languages'):
            selected_languages = [x.strip().lower()
                                  for x in reversed(arg.split(','))
                                  if is_valid(x)]
        if opt in ('-w', '--language-weight'):
            try:
                language_weight = int(arg.strip())
                if language_weight <= 0:
                    print(
                        'language-weight must be a positive integer',
                        file=sys.stderr)
                    print_help()
                    sys.exit(1)
            except ValueError:
                print('invalid value for language-weight', file=sys.stderr)
                print_help()
                sys.exit(1)
        prioritize_languages |= opt == '--prioritize-languages'
        filter_bios |= opt in ('--no-bios', '--no-all')
        filter_program |= opt in ('--no-program', '--no-all')
        filter_enhancement_chip |= opt in ('--no-enhancement-chip', '--no-all')
        filter_proto |= opt in ('--no-proto', '--no-all')
        filter_beta |= opt in ('--no-beta', '--no-all')
        filter_demo |= opt in ('--no-demo', '--no-all')
        filter_sample |= opt in ('--no-sample', '--no-all')
        filter_pirate |= opt in ('--no-pirate', '--no-all')
        filter_promo |= opt in ('--no-promo', '--no-all')
        filter_unlicensed |= opt == '--no-unlicensed'
        all_regions |= opt == '--all-regions'
        all_regions_with_lang |= opt == '--all-regions-with-lang'
        revision_asc |= opt == '--early-revisions'
        version_asc |= opt == '--early-versions'
        verbose |= opt in ('-v', '--verbose')
        ignore_case |= opt == '--ignore-case'
        regex |= opt == '--regex'
        if opt == '--separator':
            sep = arg.strip()
        input_order |= opt == '--input-order'
        prefer_parents |= opt == '--prefer-parents'
        prefer_prereleases |= opt == '--prefer-prereleases'
        if opt in ('-d', '--dat'):
            dat_file = os.path.expanduser(arg.strip())
            if not os.path.isfile(dat_file):
                print('invalid DAT file: %s' % dat_file, file=sys.stderr)
                print_help()
                sys.exit(1)
        if opt in ('-e', '--extension'):
            file_extension = arg.strip().lstrip(os.path.extsep)
        use_hashes |= opt == '--use-hashes'
        if opt == '--prefer':
            prefer_str = arg
        if opt == '--avoid':
            avoid_str = arg
        if opt == '--exclude':
            exclude_str = arg
        if opt == '--exclude-after':
            exclude_after_str = arg
        if opt in ('-i', '--input-dir'):
            input_dir = os.path.expanduser(arg.strip())
            if not os.path.isdir(input_dir):
                print(
                    'invalid input directory: %s' % input_dir,
                    file=sys.stderr)
                print_help()
                sys.exit(1)
        if opt in ('-o', '--output-dir'):
            output_dir = os.path.expanduser(arg.strip())
            if not os.path.isdir(output_dir):
                try:
                    os.makedirs(output_dir)
                except OSError:
                    print(
                        'invalid output DIR: %s' % output_dir,
                        file=sys.stderr)
                    print_help()
                    sys.exit(1)
        debug |= opt == '--debug'
        NO_WARNING |= opt == '--no-warning'
        if debug:
            verbose = True
        move |= opt == '--move'
        if opt == '--chunk-size':
            global CHUNK_SIZE
            CHUNK_SIZE = int(arg)
        if opt == '--threads':
            global THREADS
            THREADS = int(arg)
    if file_extension and use_hashes:
        print('extensions cannot be used with hashes', file=sys.stderr)
        print_help()
        sys.exit(1)
    if not input_dir and use_hashes:
        print('hashes can only be used with an input file', file=sys.stderr)
        print_help()
        sys.exit(1)
    if not dat_file:
        print('DAT file is required', file=sys.stderr)
        print_help()
        sys.exit(1)
    if not selected_regions:
        print('invalid region selection', file=sys.stderr)
        print_help()
        sys.exit(1)
    if (revision_asc or version_asc) and input_order:
        print(
            'early-revisions and early-versions are mutually exclusive '
            'with input-order',
            file=sys.stderr)
        print_help()
        sys.exit(1)
    if (revision_asc or version_asc) and prefer_parents:
        print(
            'early-revisions and early-versions are mutually exclusive '
            'with prefer-parents',
            file=sys.stderr)
        print_help()
        sys.exit(1)
    if prefer_parents and input_order:
        print(
            'prefer-parents is mutually exclusive with input-order',
            file=sys.stderr)
        print_help()
        sys.exit(1)
    if output_dir and not input_dir:
        print('output-dir requires an input-dir', file=sys.stderr)
        print_help()
        sys.exit(1)
    if ignore_case and not prefer_str and not avoid_str and not exclude_str:
        print(
            "ignore-case only works if there's a prefer, "
            "avoid or exclude list too",
            file=sys.stderr)
        print_help()
        sys.exit(1)
    if regex and not prefer_str and not avoid_str and not exclude_str:
        print(
            "regex only works if there's a prefer, avoid or exclude list too",
            file=sys.stderr)
        print_help()
        sys.exit(1)
    if all_regions and all_regions_with_lang:
        print(
            'all-regions is mutually exclusive with all-regions-with-lang',
            file=sys.stderr)
        print_help()
        sys.exit()
    try:
        prefer = parse_list(prefer_str, ignore_case, regex, sep)
    except (re.error, OSError) as e:
        print('invalid prefer list: %s' % e, file=sys.stderr)
        print_help()
        sys.exit(1)
    try:
        avoid = parse_list(avoid_str, ignore_case, regex, sep)
    except (re.error, OSError) as e:
        print('invalid avoid list: %s' % e, file=sys.stderr)
        print_help()
        sys.exit(1)
    try:
        exclude = parse_list(exclude_str, ignore_case, regex, sep)
    except (re.error, OSError) as e:
        print('invalid exclude list: %s' % e, file=sys.stderr)
        print_help()
        sys.exit(1)
    try:
        exclude_after = parse_list(exclude_after_str, ignore_case, regex, sep)
    except (re.error, OSError) as e:
        print('invalid exclude-after list: %s' % e, file=sys.stderr)
        print_help()
        sys.exit(1)

    validate_dat(dat_file, use_hashes)

    hash_index: Dict[str, str] = {}
    if use_hashes and input_dir:
        hash_index = index_files(input_dir, dat_file)

    parsed_games = parse_games(
        dat_file,
        filter_bios,
        filter_program,
        filter_enhancement_chip,
        filter_pirate,
        filter_promo,
        filter_unlicensed,
        filter_proto,
        filter_beta,
        filter_demo,
        filter_sample,
        exclude)

    if verbose:
        region_text = 'Best region match'
        lang_text = 'Best language match'
        parents_text = 'Parent ROMs'
        index_text = 'Input order'
        filters = [
            (filter_bios, 'BIOSes'),
            (filter_program, 'Programs'),
            (filter_enhancement_chip, 'Enhancement Chips'),
            (filter_proto, 'Prototypes'),
            (filter_beta, 'Betas'),
            (filter_demo, 'Demos'),
            (filter_sample, 'Samples'),
            (filter_unlicensed, 'Unlicensed ROMs'),
            (filter_pirate, 'Pirate ROMs'),
            (filter_promo, 'Promo ROMs'),
            (bool(exclude_str), 'Excluded ROMs by name'),
            (bool(exclude_after_str), 'Excluded ROMs by name (after selection)')
        ]
        enabled_filters = ['\t%d. %s\n' % (i + 1, filters[i][1])
                           for i in range(0, len(filters)) if filters[i][0]]
        if enabled_filters:
            print(
                'Filtering out:\n'
                + "".join(enabled_filters),
                file=sys.stderr)
        print(
            'Sorting with the following criteria:\n'
            '\t1. Good dumps\n'
            '\t2. %s\n'
            '\t3. Non-avoided items%s\n'
            '\t4. %s\n'
            '\t5. %s\n'
            '\t6. %s\n'
            '\t7. %s\n'
            '\t8. Preferred items%s\n'
            '\t9. %s revision\n'
            '\t10. %s version\n'
            '\t11. Latest sample\n'
            '\t12. Latest demo\n'
            '\t13. Latest beta\n'
            '\t14. Latest prototype\n'
            '\t15. Most languages supported\n'
            '\t16. Parent ROMs\n' %
            ('Prelease ROMs' if prefer_prereleases else 'Released ROMs',
             '' if avoid else ' (Ignored)',
             lang_text if prioritize_languages else region_text,
             region_text if prioritize_languages else lang_text,
             parents_text if prefer_parents else parents_text + ' (Ignored)',
             index_text if input_order else index_text + ' (Ignored)',
             '' if prefer else ' (Ignored)',
             'Earliest' if revision_asc else 'Latest',
             'Earliest' if version_asc else 'Latest'),
            file=sys.stderr)

    key_generator = GameEntryKeyGenerator(
        prioritize_languages,
        prefer_prereleases,
        prefer_parents,
        input_order,
        prefer,
        avoid)
    for key in parsed_games:
        games = parsed_games[key]
        pad_values(
            games,
            GameEntryHelper.get_version,
            GameEntryHelper.set_version)
        pad_values(
            games,
            GameEntryHelper.get_revision,
            GameEntryHelper.set_revision)
        pad_values(
            games,
            GameEntryHelper.get_sample,
            GameEntryHelper.set_sample)
        pad_values(
            games,
            GameEntryHelper.get_demo,
            GameEntryHelper.set_demo)
        pad_values(
            games,
            GameEntryHelper.get_beta,
            GameEntryHelper.set_beta)
        pad_values(
            games,
            GameEntryHelper.get_proto,
            GameEntryHelper.set_proto)
        set_scores(
            games,
            selected_regions,
            selected_languages,
            language_weight,
            revision_asc,
            version_asc)
        games.sort(key=key_generator.generate)
        if verbose:
            print(
                'Candidate order for [%s]: %s' % (key, [g.name for g in games]),
                file=sys.stderr)

    for game, entries in parsed_games.items():
        if not all_regions:
            entries = [x for x in entries if x.score.region != UNSELECTED
                       or (all_regions_with_lang and x.score.languages < 0)]
        if debug:
            print(
                'Handling candidates for game [%s]: %s' % (game, entries),
                file=sys.stderr)
        size = len(entries)
        for i in range(0, size):
            entry = entries[i]
            if check_in_pattern_list(entry.name, exclude_after):
                break
            if use_hashes:
                copied_files = set()
                num_roms = len(entry.roms)
                for entry_rom in entry.roms:
                    digest = entry_rom.sha1.lower()
                    rom_input_path = hash_index[digest]
                    if rom_input_path:
                        is_zip_file = is_zip(rom_input_path)
                        file = file_relative_to_input(rom_input_path, input_dir)
                        if not output_dir:
                            if rom_input_path not in copied_files:
                                print(file)
                                copied_files.add(rom_input_path)
                        elif rom_input_path not in copied_files:
                            if not is_zip_file \
                                    and (num_roms > 1 or os.path.sep in file):
                                rom_output_dir = os.path.join(
                                    output_dir,
                                    entry.name)
                                os.makedirs(rom_output_dir, exist_ok=True)
                            else:
                                rom_output_dir = output_dir
                            if is_zip_file:
                                rom_output_path = os.path.join(
                                    rom_output_dir,
                                    add_extension(entry.name, 'zip'))
                            else:
                                rom_output_path = os.path.join(
                                    rom_output_dir,
                                    entry_rom.name)
                            transfer_file(
                                rom_input_path,
                                rom_output_path,
                                move)
                            copied_files.add(rom_input_path)
                    else:
                        if not NO_WARNING:
                            if verbose:
                                print(
                                    'WARNING [%s]: ROM file [%s] for candidate '
                                    '[%s] not found' %
                                    (game, entry_rom.name, entry.name),
                                    file=sys.stderr)
                            else:
                                print(
                                    'WARNING: ROM file [%s] for candidate [%s] '
                                    'not found' % (entry_rom.name, entry.name),
                                    file=sys.stderr)
                if copied_files:
                    break
                elif not NO_WARNING and i == size - 1:
                    print(
                        'WARNING: no eligible candidates for [%s] '
                        'have been found!' % game,
                        file=sys.stderr)
            elif input_dir:
                file_name = add_extension(entry.name, file_extension)
                full_path = os.path.join(input_dir, file_name)
                if os.path.isfile(full_path):
                    if output_dir:
                        transfer_file(full_path, output_dir, move)
                    else:
                        print(file_name)
                    break
                elif os.path.isdir(full_path):
                    for entry_rom in entry.roms:
                        rom_input_path = os.path.join(full_path, entry_rom.name)
                        if os.path.isfile(rom_input_path):
                            if output_dir:
                                rom_output_dir = os.path.join(
                                    output_dir,
                                    file_name)
                                os.makedirs(rom_output_dir, exist_ok=True)
                                transfer_file(
                                    rom_input_path,
                                    rom_output_dir,
                                    move)
                                shutil.copystat(full_path, rom_output_dir)
                            else:
                                print(file_name + os.path.sep + entry_rom.name)
                        elif not NO_WARNING:
                            if verbose:
                                print(
                                    'WARNING [%s]: ROM file [%s] for candidate '
                                    '[%s] not found' %
                                    (game, entry_rom.name, file_name),
                                    file=sys.stderr)
                            else:
                                print(
                                    'WARNING: ROM file [%s] for candidate [%s] '
                                    'not found' % (entry_rom.name, file_name),
                                    file=sys.stderr)
                    break
                elif not NO_WARNING:
                    if verbose:
                        print(
                            'WARNING [%s]: candidate [%s] not found, '
                            'trying next one' % (game, file_name),
                            file=sys.stderr)
                    else:
                        print(
                            'WARNING: candidate [%s] not found, '
                            'trying next one' % file_name,
                            file=sys.stderr)
                    if i == size - 1:
                        print(
                            'WARNING: no eligible candidates for [%s] '
                            'have been found!' % game,
                            file=sys.stderr)
            else:
                print(add_extension(entry.name, file_extension))
                break


def add_extension(file_name: str, file_extension: str) -> str:
    if file_extension:
        return file_name + os.path.extsep + file_extension
    return file_name


def file_relative_to_input(file: str, input_dir: str) -> str:
    return file.replace(input_dir, '', 1).lstrip(os.path.sep)


def parse_list(
        arg_str: str,
        ignore_case: bool,
        regex: bool,
        separator: str) -> List[Pattern]:
    if arg_str:
        if arg_str.startswith(FILE_PREFIX):
            file = (arg_str[len(FILE_PREFIX):]).strip()
            file = os.path.expanduser(file)
            if not os.path.isfile(file):
                raise OSError('invalid file: %s' % file)
            arg_list = [x.strip() for x in open(file) if is_valid(x)]
        else:
            arg_list = [x.strip() for x in arg_str.split(separator)
                        if is_valid(x)]
        if ignore_case:
            return [re.compile(x if regex else re.escape(x), re.IGNORECASE)
                    for x in arg_list if is_valid(x)]
        else:
            return [re.compile(x if regex else re.escape(x))
                    for x in arg_list if is_valid(x)]
    return []


def set_scores(
        games: List[GameEntry],
        selected_regions: List[str],
        selected_languages: List[str],
        language_weight: int,
        revision_asc: bool,
        version_asc: bool) -> None:
    for game in games:
        region_score = get_index(selected_regions, game.region, UNSELECTED)
        languages_score = sum([
            (get_index(selected_languages, lang, -1) + 1) * -language_weight
            for lang in game.languages])
        revision_int = to_int_list(
            game.revision,
            1 if revision_asc else -1)
        version_int = to_int_list(game.version, 1 if version_asc else -1)
        sample_int = to_int_list(game.sample, -1)
        demo_int = to_int_list(game.demo, -1)
        beta_int = to_int_list(game.beta, -1)
        proto_int = to_int_list(game.proto, -1)
        game.score = Score(
            region_score,
            languages_score,
            revision_int,
            version_int,
            sample_int,
            demo_int,
            beta_int,
            proto_int)


def transfer_file(
        input_path: str,
        output_path: str,
        move: bool) -> None:
    try:
        if move:
            print('Moving [%s] to [%s]' % (input_path, output_path))
            shutil.move(input_path, output_path)
        else:
            print('Copying [%s] to [%s]' % (input_path, output_path))
            shutil.copy2(input_path, output_path)
    except OSError as e:
        print(
            'Error while reading file: %s\033[K' % e,
            file=sys.stderr)


def print_help():
    print(
        'Usage: python3 %s [options] -d input_file.dat' % sys.argv[0],
        file=sys.stderr)
    print('Options:', file=sys.stderr)
    print('\n# ROM selection and file manipulation:', file=sys.stderr)
    print(
        '\t-r,--regions=REGIONS\t'
        'A list of regions separated by commas.'
        '\n\t\t\t\t'
        'Ex.: -r USA,EUR,JPN',
        file=sys.stderr)
    print(
        '\t-l,--languages=LANGS\t'
        'An optional list of languages separated by commas.'
        '\n\t\t\t\t'
        'Ex.: -l en,es,ru',
        file=sys.stderr)
    print(
        '\t-d,--dat=DAT_FILE\t'
        'The DAT file to be used'
        '\n\t\t\t\t'
        'Ex.: -d snes.dat',
        file=sys.stderr)
    print(
        '\t-i,--input-dir=PATH\t'
        'Provides an input directory (i.e.: where your ROMs are)'
        '\n\t\t\t\t'
        'Ex.: -i "C:\\Users\\John\\Downloads\\Emulators\\SNES\\ROMs"',
        file=sys.stderr)
    print(
        '\t-e,--extension=EXT\t'
        'ROM names will use this extension.'
        '\n\t\t\t\t'
        'Ex.: -e zip',
        file=sys.stderr)
    print(
        '\t-o,--output-dir=PATH\t'
        'If provided, ROMs will be copied to an output directory'
        '\n\t\t\t\t'
        'Ex.: -o "C:\\Users\\John\\Downloads\\Emulators\\SNES\\ROMs\\1G1R"',
        file=sys.stderr)
    print(
        '\t--move\t\t\t'
        'If set, ROMs will be moved, instead of copied, '
        'to the output directory',
        file=sys.stderr)
    print(
        '\t--use-hashes\t\t'
        'If set, ROM file hashes are going to be used to identify candidates',
        file=sys.stderr)
    print(
        '\t--threads=THREADS\t'
        'When using hashes, sets the number of I/O threads to be used to read '
        'files'
        '\n\t\t\t\t'
        'Default: default Python thread pool size',
        file=sys.stderr)
    print(
        '\t--chunk-size\t\t'
        'When using hashes, sets the chunk size for buffered I/O operations '
        '(in bytes)'
        '\n\t\t\t\t'
        'Default: 33554432 bytes (32 MB)',
        file=sys.stderr)
    print('\n# Filtering:', file=sys.stderr)
    print(
        '\t--no-bios\t\t'
        'Filter out BIOSes',
        file=sys.stderr)
    print(
        '\t--no-program\t\t'
        'Filter out Programs and Test Programs',
        file=sys.stderr)
    print(
        '\t--no-enhancement-chip\t'
        'Filter out Ehancement Chips',
        file=sys.stderr)
    print(
        '\t--no-proto\t\t'
        'Filter out prototype ROMs',
        file=sys.stderr)
    print(
        '\t--no-beta\t\t'
        'Filter out beta ROMs',
        file=sys.stderr)
    print(
        '\t--no-demo\t\t'
        'Filter out demo ROMs',
        file=sys.stderr)
    print(
        '\t--no-sample\t\t'
        'Filter out sample ROMs',
        file=sys.stderr)
    print(
        '\t--no-pirate\t\t'
        'Filter out pirate ROMs',
        file=sys.stderr)
    print(
        '\t--no-promo\t\t'
        'Filter out promotion ROMs',
        file=sys.stderr)
    print(
        '\t--no-all\t\t'
        'Apply all filters above (WILL STILL ALLOW UNLICENSED ROMs)',
        file=sys.stderr)
    print(
        '\t--no-unlicensed\t\t'
        'Filter out unlicensed ROMs',
        file=sys.stderr)
    print(
        '\t--all-regions\t\t'
        'Includes files of unselected regions, if a selected one is not '
        'available',
        file=sys.stderr)
    print(
        '\t--all-regions-with-lang\t'
        'Same as --all-regions, but only if a ROM has at least one selected '
        'language',
        file=sys.stderr)
    print('\n# Adjustment and customization:', file=sys.stderr)
    print(
        '\t-w,--language-weight=N\t'
        'The degree of priority the first selected languages receive over the '
        'latter ones.'
        '\n\t\t\t\t'
        'Default: 3',
        file=sys.stderr)
    print(
        '\t--prioritize-languages\t'
        'If set, ROMs matching more languages will be prioritized over ROMs '
        'matching regions',
        file=sys.stderr)
    print(
        '\t--early-revisions\t'
        'ROMs of earlier revisions will be prioritized',
        file=sys.stderr)
    print(
        '\t--early-versions\t'
        'ROMs of earlier versions will be prioritized',
        file=sys.stderr)
    print(
        '\t--input-order\t\t'
        'ROMs will be prioritized by the order they '
        'appear in the DAT file',
        file=sys.stderr)
    print(
        '\t--prefer-parents\t'
        'Parent ROMs will be prioritized over clones',
        file=sys.stderr)
    print(
        '\t--prefer-prereleases\t'
        'Prerelease (Beta, Proto, etc.) ROMs will be prioritized',
        file=sys.stderr)
    print(
        '\t--prefer=WORDS\t\t'
        'ROMs containing these words will be preferred'
        '\n\t\t\t\t'
        'Ex.: --prefer "Virtual Console,GameCube"'
        '\n\t\t\t\t'
        'Ex.: --prefer "file:prefer.txt" ',
        file=sys.stderr)
    print(
        '\t--avoid=WORDS\t\t'
        'ROMs containing these words will be avoided (but not excluded).'
        '\n\t\t\t\t'
        'Ex.: --avoid "Virtual Console,GameCube"'
        '\n\t\t\t\t'
        'Ex.: --avoid "file:avoid.txt" ',
        file=sys.stderr)
    print(
        '\t--exclude=WORDS\t\t'
        'ROMs containing these words will be excluded.'
        '\n\t\t\t\t'
        'Ex.: --exclude "Virtual Console,GameCube"'
        '\n\t\t\t\t'
        'Ex.: --exclude "file:exclude.txt"',
        file=sys.stderr)
    print(
        '\t--exclude-after=WORDS\t'
        'If the best candidate contains these words, skip all candidates.'
        '\n\t\t\t\t'
        'Ex.: --exclude-after "Virtual Console,GameCube"'
        '\n\t\t\t\t'
        'Ex.: --exclude-after "file:exclude-after.txt"',
        file=sys.stderr)
    print(
        '\t--ignore-case\t\t'
        'If set, the avoid and exclude lists will be case-insensitive',
        file=sys.stderr)
    print(
        '\t--regex\t\t\t'
        'If set, the avoid and exclude lists are used as regular expressions',
        file=sys.stderr)
    print(
        '\t--separator=SEP\t\t'
        'Provides a separator for the avoid, exclude & exclude-after options.'
        '\n\t\t\t\t'
        'Default: ","',
        file=sys.stderr)
    print('\n# Help and debugging:', file=sys.stderr)
    print(
        '\t-h,--help\t\t'
        'Prints this usage message',
        file=sys.stderr)
    print(
        '\t-v,--verbose\t\t'
        'Prints more messages (useful when troubleshooting)',
        file=sys.stderr)
    print(
        '\t--debug\t\t\t'
        'Prints even more messages (useful when troubleshooting)',
        file=sys.stderr)
    print(
        '\t--no-warning\t\t'
        'Supresses all warnings',
        file=sys.stderr)
    print(
        '\n# See https://github.com/andrebrait/1g1r-romset-generator/wiki '
        'for more details',
        file=sys.stderr)


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except KeyboardInterrupt:
        QUIT = True
        if PROGRESSBAR:
            with PROGRESSBAR.lock:
                sys.exit('')
        else:
            sys.exit('')
