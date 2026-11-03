import requests
import json
import xml.etree.ElementTree as ET
import prsv_tools.utility.api as prsvapi
import prsv_tools.utility.cli as prsvcli
import logging
import sys
import pandas as pd
import re
import concurrent.futures
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import List, Dict, Any, Tuple, Optional

logger = logging.getLogger(__name__)

def parse_args():
    parser = prsvcli.Parser()
    parser.add_argument(
        "--credentials",
        type=str,
        required=True,
        help="Which set of credentials to use.",
    )
    parser.add_argument(
        "--uuid",
        type=str,
        nargs='*',
        help="SO uuids to export. Accepts multiple values, ie. 1234 1242 4321, etc."
    )
    parser.add_argument(
        "--ami_id",
        "-ami",
        type=str,
        nargs='*',
        help="AMI ids to export. Accepts multiple values, ie. 1234 1242 4321, etc."
    )
    parser.add_argument(
        "--digarch_id",
        "-da",
        type=str,
        nargs='*',
        help="DigArch id to export. Accepts multiple values, ie. M1234_ER_1 M1242_ER_1, etc."
    )
    parser.add_argument(
        "--output_dir",
        "-o",
        type=str,
        required=False,
        help="Directory to save reports. Defaults to current directory."
    )
    parser.add_argument(
        "--daterange",
        type=str,
        nargs=2,
        metavar=('START_DATE', 'END_DATE'),
        help="Date range (YYYY-MM-DD YYYY-MM-DD) for both AMI & DigArch pkgs."
    )
    parser.add_argument(
        "--dataframe",
        "-df",
        required=False,
        action='store_true',
        help="Flag to return dataframe instead of csv."
    )
    return parser.parse_args()

def requests_retry_session(
    retries=3,
    backoff_factor=0.3,
    status_forcelist=(500, 502, 504),
    session=None,
):
    """Creates requests session w/ retry logic."""
    session = session or requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

def search_preservica_api(
    accesstoken: str, query_params: dict, parentuuid: str, session: requests.Session
) -> requests.Response:
    base_url = "https://nypl.preservica.com/api/content/search-within"
    
    params = {
        'q': json.dumps(query_params),
        'parenthierarchy': parentuuid,
        'start': 0,
        'max': -1,
        'metadata': "''" 
    }
    
    headers = {
        "Preservica-Access-Token": accesstoken,
        "Accept": "application/json",
    }
    
    try:
        res = session.get(base_url, headers=headers, params=params, timeout=30)
        res.raise_for_status()
        return res
    except requests.exceptions.RequestException as e:
        logger.error(f"API search request failed: {e}")
        if hasattr(e, 'response') and e.response is not None:
             logger.error(f"Response Body: {e.response.text}")
        return None

def get_single_ami_uuid(accesstoken: str, pkg_id: str, parentuuid: str, session: requests.Session, silent=False) -> str:
    """Get AMI uuid based on pkg id."""
    query_params = {
        "q": "",
        "fields": [
            {"name": "xip.title", "values": [pkg_id]},
            {"name": "xip.identifier", "values": ["DigitizedAMIContainer"]}
        ]
    }
    res = search_preservica_api(accesstoken, query_params, parentuuid, session)
    if res:
        try:
            json_obj = res.json()
            if json_obj.get("success") and json_obj.get("value", {}).get("totalHits") > 0:
                obj_id = json_obj["value"]["objectIds"][0]
                return obj_id[-36:]
        except (json.JSONDecodeError, IndexError, KeyError) as e:
            logger.error(f"Failed to parse JSON response or find uuid: {e}")
            logger.error(f"Response text: {res.text[:200]}")
    else:
        if not silent:
            logger.warning(f"No search results found for AMI ID in {parentuuid} folder: {pkg_id}")
        return None


def get_digarch_uuids(accesstoken: str, pkg_id: str, parentuuid: str, session: requests.Session) -> str:
    """Get DigArch uuid based on DigArch id."""
    col_id_match = re.search(r"(M\d+)_(ER|DI|EM)_\d+", pkg_id)
    if not col_id_match:
        logger.error(f"Could not parse collection id from DigArch id: {pkg_id}")
        return None
    col_id = col_id_match.group(1)
    query_params = {
        "q": "",
        "fields": [
            {"name": "xip.title", "values": [pkg_id]},
            {"name": "spec.specCollectionID", "values": [col_id]},
        ],
    }
    response = search_preservica_api(accesstoken, query_params, parentuuid, session)
    if response:
        try:
            json_obj = response.json()
            if json_obj.get("success") and json_obj.get("value", {}).get("totalHits") > 0:
                obj_id = json_obj["value"]["objectIds"][0]
                return obj_id[-36:]
        except (json.JSONDecodeError, IndexError, KeyError) as e:
            logger.error(f"Failed to parse JSON response or find uuid: {e}")
            logger.error(f"Response text: {response.text[:200]}")

    logger.warning(f"No search results found for DigArch ID in DigArch folder: {pkg_id}")
    return None

def get_by_date_uuids(accesstoken: str, start_date: str, end_date: str, parentuuid: str, identifier: str, session: requests.Session) -> list:
    """Get pkg uuids based on date range &identifier."""
    query_params = {
        "q": "",
        "fields": [
            {"name": "xip.created", "values": [f"{start_date} - {end_date}"]},
            {"name": "xip.identifier", "values": [identifier]}
        ]
    }
    response = search_preservica_api(accesstoken, query_params, parentuuid, session)
    uuids = []
    if response:
        try:
            json_obj = response.json()
            if json_obj.get("success") and json_obj.get("value", {}).get("totalHits") > 0:
                for obj_id in json_obj["value"]["objectIds"]:
                    uuids.append(obj_id[-36:])
        except (json.JSONDecodeError, IndexError, KeyError) as e:
            logger.error(f"Failed to parse JSON response or find uuids: {e}")
            logger.error(f"Response text: {response.text[:200]}")
    
    if not uuids:
        logger.warning(f"No pkgs found for date range {start_date} - {end_date} w/ identifier {identifier}")
    
    return uuids

def get_parent_so_title(accesstoken: str, child_uuid: str, version: str, session: requests.Session) -> Optional[str]:
    url = f"https://nypl.preservica.com/api/entity/structural-objects/{child_uuid}"
    root = _get_entity_xml(accesstoken, session, url)
    
    if root is not None:
        parent_element = root.find(f".//{{http://preservica.com/XIP/v{version}}}Parent")
        if parent_element is not None and parent_element.text:
            parent_uuid = parent_element.text.strip()

            return get_pkg_title(accesstoken, parent_uuid, version, session)
            
    logger.warning(f"Could not find parent title for SO {child_uuid}")
    return None

########################
def _get_entity_xml(accesstoken: str, session: requests.Session, url: str) -> Optional[ET.Element]:
    """Helper to make a GET request and parse response."""
    headers = {
        "Preservica-Access-Token": accesstoken,
        "accept": "application/xml;charset=UTF-8"
    }
    try:
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return ET.fromstring(response.text)
    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed for URL {url}: {e}")
        return None
    except ET.ParseError as e:
        logger.error(f"Failed to parse XML from URL {url}: {e}")
        return None

########################
def get_identifiers(accesstoken: str, version: str, entity_type: str, entity_ref: str, session: requests.Session, namespaces) -> str:
    url = f"https://nypl.preservica.com/api/entity/{entity_type}/{entity_ref}/identifiers"
    root = _get_entity_xml(accesstoken, session, url)
    id_container = {}
    if root is not None:

        identifiers_list = root.findall('./entity:Identifiers/xip:Identifier', namespaces)
        
        for identifier in identifiers_list:
            id_type = identifier.find('xip:Type', namespaces)
            id_value = identifier.find('xip:Value', namespaces)
            
            if (id_type is not None and id_type.text and 
                id_value is not None and id_value.text):
                id_container[id_type.text.strip()] = id_value.text.strip()
    else:
        logger.warning("get_identifiers(): Root is None")
    
    return id_container if id_container else "Not Found"

def get_security_tag(accesstoken: str, version: str, entity_type: str, entity_ref: str, session: requests.Session, namespaces, root=None) -> str:
    """Gets sectags for SOs or IOs."""
    if root is None:
        url = f"https://nypl.preservica.com/api/entity/{entity_type}/{entity_ref}"
        root = _get_entity_xml(accesstoken, session, url)
        
    if root is not None:
        tag_element = root.find('.//xip:SecurityTag', namespaces)
        if tag_element is not None and tag_element.text:
            return tag_element.text.strip()
    return "Not Found"

def get_metadata_fragments(accesstoken: str, version: str, entity_type: str, entity_ref: str, session: requests.Session, namespaces, root=None) -> str:
    """Gets and parses all mfrags."""
    if root is None:
        entity_url = f"https://nypl.preservica.com/api/entity/{entity_type}/{entity_ref}"
        root = _get_entity_xml(accesstoken, session, entity_url)
        
    if root is None:
        return "Not Found"
    
    fragment_elements = root.findall('.//entity:AdditionalInformation/entity:Metadata/entity:Fragment', namespaces)
    
    if not fragment_elements:
        return ""

    all_fragments_data = []
    for fragment in fragment_elements:
        mfrag_url = fragment.text
        
        if not mfrag_url:
            continue

        mfrag_root = _get_entity_xml(accesstoken, session, mfrag_url)
        if mfrag_root is None:
            continue
            
        content_element = mfrag_root.find('.//xip:Content', namespaces)
        if content_element is not None and len(content_element) > 0:
            data_container = content_element[0]
            for data_element in data_container:
                value = data_element.text.strip() if data_element.text else ""
                if value:
                    all_fragments_data.append(f"{data_container.tag.split('}')[-1]}({data_element.tag.split('}')[-1]}):{value}")

    return "^".join(all_fragments_data)

def get_pkg_title(accesstoken: str, pkg_uuid: str, version: str, session: requests.Session, root=None) -> Optional[str]:
    """Gets SO title."""
    if root is None:
        url = f"https://nypl.preservica.com/api/entity/structural-objects/{pkg_uuid}"
        root = _get_entity_xml(accesstoken, session, url)
        
    if root is not None:
        title_element = root.find(f".//{{http://preservica.com/XIP/v{version}}}Title")
        if title_element is not None and title_element.text:
            return title_element.text.strip()
    
    logger.warning(f"No title found for SO {pkg_uuid}")
    return None

def get_so_children(accesstoken: str, version: str, parent_uuid: str, session: requests.Session, namespaces) -> list:
    """Gets direct children (SOs & IOs) of SO with pagination."""
    children_data = []
    start = 0
    max_count = 100
    
    while True:
        url = f"https://nypl.preservica.com/api/entity/structural-objects/{parent_uuid}/children?start={start}&max={max_count}"
        root = _get_entity_xml(accesstoken, session, url)
        
        if root is None:
            logger.error(f"Could not retrieve children for {parent_uuid} at start={start}")
            break
            
        new_children = root.findall('.//entity:Child', namespaces)
        if not new_children:
            break
            
        for child in new_children:
            children_data.append({
                'ref': child.get('ref'),
                'type': child.get('type'),
                'title': child.get('title')
            })
            
        paging = root.find('.//entity:Paging', namespaces)
        if paging is not None:
            total_results_elem = paging.find('entity:TotalResults', namespaces)
            if total_results_elem is not None and total_results_elem.text:
                total_results = int(total_results_elem.text)
                start += max_count
                if start >= total_results:
                    break
            else:
                break
        else:
            break

    return children_data

def get_co_details(accesstoken: str, version: str, co_ref: str, session: requests.Session, namespaces, root=None) -> dict:
    """Gets CO details, ie. title & parent IO."""
    if root is None:
        url = f"https://nypl.preservica.com/api/entity/content-objects/{co_ref}"
        root = _get_entity_xml(accesstoken, session, url)
        
    details = {}
    if root is not None:
        title_element = root.find('.//xip:Title', namespaces)
        parent_element = root.find('.//xip:Parent', namespaces)
        details['co_title'] = title_element.text.strip() if title_element is not None else None
        details['co_parent'] = parent_element.text.strip() if parent_element is not None else None
    
    if not details:
        logger.error(f"Failed to get details for CO {co_ref}")

    return details

def get_generation_numbers(accesstoken: str, version: str, co_ref: str, session: requests.Session, namespaces) -> list:
    """Gets CO generation number."""
    url = f"https://nypl.preservica.com/api/entity/content-objects/{co_ref}/generations"
    root = _get_entity_xml(accesstoken, session, url)
    generations = []
    
    if root is not None:
        gen_elements = root.findall('.//entity:Generations/entity:Generation', namespaces)
        for gen_element in gen_elements:
            if gen_element.text:
                generations.append(gen_element.text.split('/')[-1])
    
    if not generations:
        logger.error(f"Failed to get generation numbers for CO {co_ref}")
        
    return generations

def get_formats(accesstoken: str, version: str, co_ref: str, generation: str, session: requests.Session, namespaces, root=None) -> str:
    """Gets all CO formats."""
    if root is None:
        url = f"https://nypl.preservica.com/api/entity/content-objects/{co_ref}/generations/{generation}"
        root = _get_entity_xml(accesstoken, session, url)
        
    formats = []
    if root is not None:
        for elem in root.findall('.//xip:Formats/xip:Format/xip:PUID', namespaces):
            if elem.text:
                formats.append(elem.text)
    
    if not formats:
         logger.error(f"Failed to get formats for CO {co_ref}")

    return "^".join(formats)

def get_ingest_details(accesstoken: str, version: str, so_ref: str, session: requests.Session, namespaces) -> dict:
    """Gets SO ingest event details."""
    url = f"https://nypl.preservica.com/api/entity/structural-objects/{so_ref}/event-actions"
    root = _get_entity_xml(accesstoken, session, url)
    if root is not None:
        ingest_event = root.find('.//xip:EventAction[@commandType="command_create"]/xip:Event[@type="Ingest"]', namespaces)
        if ingest_event is not None:
            return {
                'IngestDate': ingest_event.find('xip:Date', namespaces).text,
                'IngestUser': ingest_event.find('xip:User', namespaces).text,
                'IngestWFName': ingest_event.find('xip:WorkflowName', namespaces).text,
                'IngestWFInstanceID': ingest_event.find('xip:WorkflowInstanceId', namespaces).text
            }
    
    logger.warning(f"No Ingest event found for SO {so_ref}")
    return {}

def get_representation_details(accesstoken: str, version: str, io_ref: str, session: requests.Session, namespaces) -> list:
    """Gets representation details for IO."""
    url = f"https://nypl.preservica.com/api/entity/information-objects/{io_ref}/representations"
    root = _get_entity_xml(accesstoken, session, url)
    rep_details = []

    type_counts = {}

    if root is not None:
        for rep in root.findall('.//entity:Representation', namespaces):
            base_type = rep.get('type')
            current_count = type_counts.get(base_type, 0) + 1
            type_counts[base_type] = current_count
            
            # create type name for mult preservation types: "(iii) a combined type and index (e.g. Access_2).""
            suffixed_type = f"{base_type}_{current_count}"
            
            rep_details.append({'type': suffixed_type,'name': rep.get('name', "")})
            # debug
            # print({'type': suffixed_type,'name': rep.get('name', "")})
    
    if not rep_details:
        logger.error(f"Failed to get representations for IO {io_ref}")
    return rep_details

def get_generation_details(accesstoken: str, version: str, io_ref: str, rep_type: str, session: requests.Session, namespaces) -> list:
    """Gets CO refs from representation."""
    url = f"https://nypl.preservica.com/api/entity/information-objects/{io_ref}/representations/{rep_type}"
    root = _get_entity_xml(accesstoken, session, url)
    co_refs = []
    if root is not None:
        for co in root.findall('.//xip:ContentObjects/xip:ContentObject', namespaces):
            if co.text and co.text.strip() not in co_refs:
                co_refs.append(co.text.strip())
        for co in root.findall('.//entity:ContentObjects/entity:ContentObject', namespaces):
            if co.get('ref') and co.get('ref') not in co_refs:
                co_refs.append(co.get('ref'))

    if not co_refs:
         logger.error(f"Failed to get COs for IO {io_ref}, Rep {rep_type}")

    return co_refs

def _fetch_bitstream_data(bitstream_url: str, accesstoken: str, session: requests.Session, namespaces: dict) -> dict:
    """fetch and parse a single bitstream's XML."""
    root = _get_entity_xml(accesstoken, session, bitstream_url)
    if root is None:
        return None
        
    details = {}
    filename = root.find('.//xip:Filename', namespaces)
    filesize = root.find('.//xip:FileSize', namespaces)
    
    details['filename'] = filename.text if filename is not None else None
    details['filesize'] = int(filesize.text) if filesize is not None and filesize.text else None
    details['bitstream_url'] = bitstream_url

    fixity_data = {}
    for fixity in root.findall('.//xip:Fixity', namespaces):
        alg = fixity.find('xip:FixityAlgorithmRef', namespaces)
        val = fixity.find('xip:FixityValue', namespaces)
        if alg is not None and val is not None:
            fixity_data[alg.text] = val.text
    details['fixity'] = fixity_data
    
    return details

def get_bitstream_details(accesstoken: str, version: str, co_ref: str, generation: str, session: requests.Session, namespaces, root=None) -> list:
    """Gets all bitstream details for a specific generation of a CO concurrently."""
    gen_root = root
    if gen_root is None:
        gen_url = f"https://nypl.preservica.com/api/entity/content-objects/{co_ref}/generations/{generation}"
        gen_root = _get_entity_xml(accesstoken, session, gen_url)
    
    all_bitstream_details = []
    
    if gen_root is not None:
        bitstream_elements = gen_root.findall('.//entity:Bitstream', namespaces)
        urls_to_fetch = [elem.text for elem in bitstream_elements if elem is not None and elem.text]
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                executor.submit(_fetch_bitstream_data, url, accesstoken, session, namespaces)
                for url in urls_to_fetch
            ]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result:
                    all_bitstream_details.append(result)

    if not all_bitstream_details:
        logger.warning(f"No bitstreams found for CO {co_ref}, Generation {generation}")
        
    return all_bitstream_details

def find_all_children(accesstoken: str, version: str, parent_uuid: str, so_list: list, io_list: list, session: requests.Session, namespaces):
    """Recursively finds all SO & IO children, storing IOs w/ their parent SO."""
    children = get_so_children(accesstoken, version, parent_uuid, session, namespaces)
    
    for child in children:
        ref = child['ref']
        entity_type = child['type']
        title = child['title']

        if entity_type == 'IO':
            io_list.append({'ref': ref, 'title': title, 'parent_so_ref': parent_uuid})
        elif entity_type == 'SO':
            so_list.append(ref)
            find_all_children(accesstoken, version, ref, so_list, io_list, session, namespaces)

def generate_package_dataframe(start_uuid: str, accesstoken: str, version: str, session: requests.Session, namespaces) -> Tuple[Optional[str], Optional[pd.DataFrame]]:
    """Process a single package to return a df."""

    # Fetch top level XML once
    top_url = f"https://nypl.preservica.com/api/entity/structural-objects/{start_uuid}"
    top_root = _get_entity_xml(accesstoken, session, top_url)

    pkg_title = get_pkg_title(accesstoken, start_uuid, version, session, root=top_root)
    if not pkg_title:
        logger.error(f"Could not retrieve pkg title for {start_uuid}, skipping.")
        return None, None

    logger.info(f"Processing pkg parts: {pkg_title} (uuid: {start_uuid})")


    top_level_so_mfrag = get_metadata_fragments(accesstoken, version, 'structural-objects', start_uuid, session, namespaces, root=top_root)
    top_level_identifier = get_identifiers(accesstoken, version, 'structural-objects', start_uuid, session, namespaces)

    ingest_info = get_ingest_details(accesstoken, version, start_uuid, session, namespaces)
    
    child_so_refs, io_info_list = [], []
    find_all_children(accesstoken, version, start_uuid, child_so_refs, io_info_list, session, namespaces)
    
    if not io_info_list:
        logger.warning(f"No IOs found for {pkg_title}.\n")
        return pkg_title, None

    logger.info("Creating report...")

    all_so_refs = [start_uuid] + child_so_refs
    so_details_cache = {}
    for so_ref in all_so_refs:
        if so_ref not in so_details_cache:
            so_url = f"https://nypl.preservica.com/api/entity/structural-objects/{so_ref}"
            so_root = _get_entity_xml(accesstoken, session, so_url)

            so_details_cache[so_ref] = {
                'title': get_pkg_title(accesstoken, so_ref, version, session, root=so_root),
                'security_tag': get_security_tag(accesstoken, version, 'structural-objects', so_ref, session, namespaces, root=so_root),
                'mfrags': get_metadata_fragments(accesstoken, version, 'structural-objects', so_ref, session, namespaces, root=so_root),
                'ids': get_identifiers(accesstoken, version, 'structural-objects', so_ref, session, namespaces)
            }

    all_data = []
    io_details_cache = {}
    filepath_counts = defaultdict(int)

    for io_info in io_info_list:
        io_ref = io_info['ref']
        io_title = io_info['title']
        parent_so_ref = io_info['parent_so_ref']

        if io_ref not in io_details_cache:
            io_url = f"https://nypl.preservica.com/api/entity/information-objects/{io_ref}"
            io_root = _get_entity_xml(accesstoken, session, io_url)

            io_details_cache[io_ref] = {
                'security_tag': get_security_tag(accesstoken, version, 'information-objects', io_ref, session, namespaces, root=io_root),
                'mfrags': get_metadata_fragments(accesstoken, version, 'information-objects', io_ref, session, namespaces, root=io_root),
                'ids': get_identifiers(accesstoken, version, 'information-objects', io_ref, session, namespaces)
            }
            
        parent_so_title = so_details_cache.get(parent_so_ref, {}).get('title')
        so_security_tag = so_details_cache.get(parent_so_ref, {}).get('security_tag')
        so_mfrag = so_details_cache.get(parent_so_ref, {}).get('mfrags')
        so_id = so_details_cache.get(parent_so_ref, {}).get('ids')
        
        io_security_tag = io_details_cache[io_ref]['security_tag']
        io_mfrag = io_details_cache[io_ref]['mfrags']
        io_id = io_details_cache[io_ref]['ids']
        
        local_path_type = "contents" if parent_so_title and "content" in parent_so_title.lower() else "metadata"
        local_path = f"{pkg_title}_{local_path_type}"

        representations = get_representation_details(accesstoken, version, io_ref, session, namespaces)

        for rep in representations:
            co_refs = get_generation_details(accesstoken, version, io_ref, rep['type'], session, namespaces)
            
            for co_ref in co_refs:
                generations = get_generation_numbers(accesstoken, version, co_ref, session, namespaces)
                
                co_url = f"https://nypl.preservica.com/api/entity/content-objects/{co_ref}"
                co_root = _get_entity_xml(accesstoken, session, co_url)
                
                co_details = get_co_details(accesstoken, version, co_ref, session, namespaces, root=co_root)
                co_id = get_identifiers(accesstoken, version, 'content-objects', co_ref, session, namespaces)
                
                if not generations:
                    logger.warning(f"Could not retrieve generations for CO: {co_ref}, skipping.")
                    continue
                
                for generation in generations:
                    gen_url = f"https://nypl.preservica.com/api/entity/content-objects/{co_ref}/generations/{generation}"
                    gen_root = _get_entity_xml(accesstoken, session, gen_url)
                    
                    bitstreams = get_bitstream_details(accesstoken, version, co_ref, generation, session, namespaces, root=gen_root)
                    
                    if bitstreams and co_details:
                        formats = get_formats(accesstoken, version, co_ref, generation, session, namespaces, root=gen_root)
                        
                        for bitstream in bitstreams:
                            path_key = (rep['type'], bitstream.get('filename'))
                            filepath_counts[path_key] += 1
                            path_instance = filepath_counts[path_key]

                            base_path_name = f"Representation_{rep['type']}"
                            file_path = (f"{base_path_name}_{path_instance}/{co_details.get('co_title')}"
                                         f"/Generation_{generation}/{bitstream.get('filename')}")

                            fixity = bitstream.get('fixity', {})
                            row = {
                                'Package Title': pkg_title,
                                'Local_Path': local_path, 'IO Ref': io_ref, 'IO Title': io_title,
                                'Parent SO Identifier': top_level_identifier, 'SO Identifier': so_id, 
                                'IO Identifier': io_id, 'CO Identifier': co_id, 
                                'Parent Ref': parent_so_ref, 'CO Ref': co_ref,
                                'CO Title': co_details.get('co_title'), 'CO Parent': co_details.get('co_parent'),
                                'Representation Type': rep['type'], 'Generation': generation,
                                'File Path': file_path, 'File Name': bitstream.get('filename'),
                                'File Size': bitstream.get('filesize'),
                                'SHA512': 'SHA512' if 'SHA512' in fixity else 'NA',
                                'SHA512ChecksumVal': fixity.get('SHA512', 'NA'),
                                'SHA256': 'SHA256' if 'SHA256' in fixity else 'NA',
                                'SHA256ChecksumVal': fixity.get('SHA256', 'NA'),
                                'SHA1': 'SHA1' if 'SHA1' in fixity else 'NA',
                                'SHA1ChecksumVal': fixity.get('SHA1', 'NA'),
                                'MD5': 'MD5' if 'MD5' in fixity else 'NA',
                                'MD5ChecksumVal': fixity.get('MD5', 'NA'), 'Formats': formats,
                                **ingest_info,
                                'SO Security Tag': so_security_tag,
                                'Parent SO mFrag': top_level_so_mfrag,
                                'SO mFrag': so_mfrag,
                                'IO Security Tag': io_security_tag,
                                'IO mFrag': io_mfrag
                            }
                            all_data.append(row)
                    else:
                        logger.warning(f"Could not retrieve bitstreams or details for CO: {co_ref}, Generation {generation}, skipping.")

    if not all_data:
        logger.info(f"No data found to write to file for {pkg_title}.\n")
        return pkg_title, None

    column_order = [
        'Package Title', 'Local_Path', 'IO Ref', 'IO Title', 'Parent SO Identifier',
        'SO Identifier', 'IO Identifier', 'CO Identifier', 'Parent Ref', 'CO Ref',
        'CO Title', 'CO Parent', 'Representation Type', 'Generation', 'File Path',
        'File Name', 'File Size', 'SHA512', 'SHA512ChecksumVal', 'SHA256',
        'SHA256ChecksumVal', 'SHA1', 'SHA1ChecksumVal', 'MD5', 'MD5ChecksumVal',
        'Formats', 'IngestDate', 'IngestUser', 'IngestWFName', 'IngestWFInstanceID',
        'SO Security Tag', 'Parent SO mFrag', 'SO mFrag', 'IO Security Tag', 'IO mFrag'
    ]
    df = pd.DataFrame(all_data)

    for col in column_order:
        if col not in df.columns:
            df[col] = ''
    df = df[column_order]
    
    return pkg_title, df

def create_report(
    credentials: str,
    output_dir: Optional[str] = None,
    uuids: Optional[List[str]] = None,
    ami_id: Optional[List[str]] = None,
    digarch_ids: Optional[List[str]] = None,
    daterange: Optional[Tuple[str, str]] = None,
    return_df: bool = False
) -> Optional[pd.DataFrame]:

    accesstoken = prsvapi.get_token(credentials)
    session = requests_retry_session()
    version = prsvapi.find_apiversion(credentials)

    if "test" in credentials:
        digarch_parent_uuid = "c0b9b47a-5552-4277-874e-092b3cc53af6"
        ami_parent_uuid = ""
    else:
        digarch_parent_uuid = "e80315bc-42f5-44da-807f-446f78621c08"
        ami_parent_uuid = "183a74b5-7247-4fb2-8184-959366bc0cbc"

    deletion_uuid = "836a114b-839a-4af8-a4e1-001f200d6d40"

    namespaces = {
        'xip': f'http://preservica.com/XIP/v{version}',
        'entity': f'http://preservica.com/EntityAPI/v{version}'
    }

    if output_dir is not None:
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

    identifiers = []
    if uuids:
        identifiers.extend(uuids)
    if ami_id:
        for id in ami_id:
            uuid = get_single_ami_uuid(accesstoken, id, ami_parent_uuid, session)
            if not uuid:
                uuid = get_single_ami_uuid(accesstoken, id, deletion_uuid, session)
                parent_title = get_parent_so_title(accesstoken, uuid, version, session)
                if uuid:
                    logger.warning(f"{id} found in Deletion Folder: '{parent_title}'")
            if uuid:
                identifiers.append(uuid)
    if digarch_ids:
        for digarch_id in digarch_ids:
            uuid = get_digarch_uuids(accesstoken, digarch_id, digarch_parent_uuid, session)
            if not uuid:
                uuid = get_digarch_uuids(accesstoken, digarch_id, deletion_uuid, session)
                parent_title = get_parent_so_title(accesstoken, uuid, version, session)
                if uuid:
                    logger.warning(f"{id} found in Deletion Folder: '{parent_title}'")
            if uuid:
                identifiers.append(uuid)
    if daterange:
        start_date, end_date = daterange
        logger.info(f"Searching for AMI and DigArch pkgs from {start_date} to {end_date}")
        ami_uuids = get_by_date_uuids(accesstoken, start_date, end_date, ami_parent_uuid, "DigitizedAMIContainer", session)
        identifiers.extend(ami_uuids)
        digarch_uuids = get_by_date_uuids(accesstoken, start_date, end_date, digarch_parent_uuid, "ERContainer", session)
        identifiers.extend(digarch_uuids)
    
    if not identifiers:
        logger.error("No valid uuids were found.")
        return None

    all_dfs = []
    for start_uuid in identifiers:
        accesstoken = prsvapi.get_token(credentials)
        
        if not return_df and output_dir is not None:
            pkg_title_check = get_pkg_title(accesstoken, start_uuid, version, session)
            if not pkg_title_check:
                logger.error(f"Could not get title for {start_uuid}, skipping.")
            else:
                output_filename = f"{pkg_title_check}_Info.csv"
                if list(output_path.rglob(output_filename)):
                    logger.info(f"Report for {pkg_title_check} already exists, skipping.")
                    continue

        pkg_title, df = generate_package_dataframe(start_uuid, accesstoken, version, session, namespaces)
        
        if df is not None:
            if return_df:
                all_dfs.append(df)
            else:
                final_output_dir = output_path
                if daterange:
                    date_folder_name = f"{daterange[0]}_{daterange[1]}"
                    final_output_dir = output_path / date_folder_name
                final_output_dir.mkdir(parents=True, exist_ok=True)
                
                output_filename = f"{pkg_title}_Info.csv"
                full_path = final_output_dir / output_filename
                df.to_csv(full_path, index=False)
                logger.info(f"Report exported to: {full_path}\n")

    if return_df:
        return pd.concat(all_dfs, ignore_index=True) if all_dfs else None
    
    return None

def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s', stream=sys.stdout)
    
    args = parse_args()

    create_report(
        credentials=args.credentials,
        output_dir=args.output_dir,
        uuids=args.uuid,
        ami_id=args.ami_id,
        digarch_ids=args.digarch_id,
        daterange=args.daterange,
        return_df=args.dataframe
    )

if __name__ == "__main__":
    main()