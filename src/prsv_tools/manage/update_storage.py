import argparse
import logging
import json
import requests
import time
import xml.etree.ElementTree as ET
from pathlib import Path
import datetime

import prsv_tools.utility.api as prsvapi
from prsv_tools.manage.create_pkg_report import requests_retry_session

def parse_args():
    parser = argparse.ArgumentParser(description="Start Update Storage (v6) workflow in Preservica.")
    parser.add_argument(
        "--credentials",
        type=str,
        required=True,
    )
    
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--pkguuid",
        "-u",
        type=str,
        help="A single package UUID to update directly."
    )
    group.add_argument(
        "--ami",
        type=str,
        help="A single package AMI ID to find and update. Requires --search-parent to be set."
    )
    group.add_argument(
        "--parentuuid",
        "-p",
        type=str,
        help="A parent folder UUID. All immediate child structural objects will be updated."
    )
    
    parser.add_argument(
        "--search-parent",
        type=str,
        help="The parent folder UUID to search within when using the --ami flag."
    )
    parser.add_argument(
        "--logpath", 
        type=Path,
        help="Directory for log files."
    )
    return parser.parse_args()

def get_environment_config(credentials: str) -> dict:
    """Configures params and workflow context."""
    cred_lower = credentials.lower()
    
    if "prod" in cred_lower:
        return {
            "environment": "production",
            "workflow_name": "Update Storage (v6)",
            "workflow_id": "95",
            "entity_param_key": "EntityRefs"
        }
    else:
        return {
            "environment": "test",
            "workflow_name": "Update Storage (v6)",
            "workflow_id": "66",
            "entity_param_key": "EntityRefs"
        }

def get_uuids_for_title(token, pkg_title, parent_uuid, session):
    """Searches for UUIDs matching a title within a specific parent using the Content API."""
    query_params = {
        "q": "", 
        "fields": [
            {"name": "xip.title", "values": [pkg_title]},
            {"name": "xip.document_type", "values": ["SO"]}
        ]
    }
    query = json.dumps(query_params)
    
    url = (
        f"https://nypl.preservica.com/api/content/search-within"
        f"?q={requests.utils.quote(query)}"
        f"&parenthierarchy={parent_uuid}"
        f"&start=0"
        f"&max=100"
        f"&metadata=''" 
    )

    headers = {"Preservica-Access-Token": token}
    uuids = []
    
    try:
        res = session.get(url, headers=headers, timeout=30)
        res.raise_for_status()
        data = res.json()

        if data.get("success") and data.get("value", {}).get("totalHits", 0) > 0:
            value = data["value"]
            object_ids = value.get("objectIds", [])
            uuids = [u[-36:] for u in object_ids]
    except Exception as e:
        logging.error(f"Error searching for {pkg_title} UUIDs: {e}")

    return uuids

def get_immediate_child_sos(accesstoken: str, version: str, parent_uuid: str, session: requests.Session) -> list:
    """Gets direct SO children utilizing correct namespaces and pagination."""
    namespaces = {
        'entity': f'http://preservica.com/EntityAPI/v{version}'
    }
    
    uuids = []
    start = 0
    max_count = 100
    
    headers = {
        "Preservica-Access-Token": accesstoken,
        "accept": "application/xml;charset=UTF-8"
    }
    
    while True:
        url = f"https://nypl.preservica.com/api/entity/structural-objects/{parent_uuid}/children?start={start}&max={max_count}"
        
        try:
            response = session.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            root = ET.fromstring(response.text)
        except requests.exceptions.RequestException as e:
            logging.error(f"API request failed for URL {url}: {e}")
            break
        except ET.ParseError as e:
            logging.error(f"Failed to parse XML from URL {url}: {e}")
            break
            
        new_children = root.findall('.//entity:Child', namespaces)
        if not new_children:
            break
            
        for child in new_children:
            if child.get('type') == 'SO':
                uuids.append(child.get('ref'))
                
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

    return uuids

def verify_workflow_active(token, correlation_token, session):
    """Checks once that the workflow exists and is active."""
    url = f"https://nypl.preservica.com/api/processmonitor/progress/{correlation_token}?includeErrors=false"
    
    headers = {
        "Preservica-Access-Token": token,
        "Accept": "application/xml;charset=UTF-8"
    }

    try:
        res = session.get(url, headers=headers)
        root = ET.fromstring(res.text)
        
        if root.tag == "Error":
            error_msg = root.find("ExtendedMessage")
            msg_text = error_msg.text if error_msg is not None else "Unknown API Error"
            logging.error(f"Workflow {correlation_token} error: {msg_text}")
            return False
            
        status_elem = root.find("{http://status.preservica.com}Status")
        status = status_elem.text if status_elem is not None else "UNKNOWN"
        
        if status == "ACTIVE":
            logging.info(f"Verified: Workflow instance with Correlation Token {correlation_token} is ACTIVE.")
            return True
        else:
            logging.warning(f"Workflow instance {correlation_token} returned status: {status}")
            return False

    except ET.ParseError:
        logging.error(f"Failed to parse XML response for token {correlation_token}.")
        return False
    except Exception as e:
        logging.error(f"Error checking workflow {correlation_token}: {e}")
        return False

def start_update_storage_workflow(token, entity_uuids, env_config, session):
    """Starts the Update Storage workflow using environment-specific parameters."""
    url = "https://nypl.preservica.com/sdb/rest/workflow/instances"
    
    headers = {
        "Preservica-Access-Token": token,
        "Content-Type": "application/xml",
        "Accept": "application/xml"
    }
    
    entity_refs_str = ",".join(entity_uuids)
    param_key = env_config["entity_param_key"]
    
    xml_payload = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<StartWorkflowRequest xmlns="http://workflow.preservica.com">
    <WorkflowContextId>{env_config['workflow_id']}</WorkflowContextId>
    <Parameter>
        <Key>{param_key}</Key>
        <Value>{entity_refs_str}</Value>
    </Parameter>
</StartWorkflowRequest>"""

    try:
        res = session.post(url, headers=headers, data=xml_payload)
        res.raise_for_status()
        
        root = ET.fromstring(res.text)
        correlation_token_elem = root.find("{http://workflow.preservica.com}CorrelationToken")
        
        if correlation_token_elem is not None and correlation_token_elem.text:
            correlation_token = correlation_token_elem.text
            logging.info(f"Successfully submitted workflow instance. Extracted Correlation Token: {correlation_token}")
            
            return verify_workflow_active(token, correlation_token, session)
        else:
            logging.error("Successfully started workflow but could not extract a Correlation Token from the response.")
            return False
            
    except ET.ParseError:
        logging.error("Workflow was triggered, but the response XML could not be parsed.")
        return False
    except Exception as e:
        logging.error(f"Error starting workflow: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Response details: {e.response.text}")
        return False

def main():
    args = parse_args()

    log_path = args.logpath if args.logpath else Path.cwd()
    log_file = log_path / f"prsv_update_storage_{datetime.datetime.now().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )
    logger = logging.getLogger(__name__)

    env_config = get_environment_config(args.credentials)
    # logger.info(f"Running script targeting: {env_config['environment'].upper()} environment (Workflow ID: {env_config['workflow_id']})")

    session = requests_retry_session()
    token = prsvapi.get_token(args.credentials)
    version = prsvapi.find_apiversion(args.credentials)
    
    target_uuids = []

    if args.pkguuid:
        logger.info(f"Targeting single package UUID: {args.pkguuid}")
        target_uuids.append(args.pkguuid)
        
    elif args.ami:
        if not args.search_parent:
            logger.error("You must provide a --search-parent UUID when searching by AMI ID.")
            return
            
        logger.info(f"Searching for package '{args.ami}' in parent {args.search_parent}")
        resolved_uuids = get_uuids_for_title(token, args.ami, args.search_parent, session)
        
        if resolved_uuids:
            logger.info(f"Found {len(resolved_uuids)} instance(s) for AMI {args.ami}")
            target_uuids.extend(resolved_uuids)
        else:
            logger.error(f"Could not find a package matching AMI {args.ami}")
            
    elif args.parentuuid:
        logger.info(f"Fetching SOs for parent UUID: {args.parentuuid}")
        resolved_uuids = get_immediate_child_sos(token, version, args.parentuuid, session)
        if resolved_uuids:
            logger.info(f"Found {len(resolved_uuids)} SOs in parent.")
            target_uuids.extend(resolved_uuids)
        else:
            logger.error(f"No SOs found or error accessing folder {args.parentuuid}")

    if not target_uuids:
        logger.warning("No valid UUIDs found to process. Exiting.")
        return

    logger.info(f"Triggering workflow for {len(target_uuids)} SOs...")
    success = start_update_storage_workflow(token, target_uuids, env_config, session)
    
    if success:
        logger.info("Completed successfully.")
    else:
        logger.error("Encountered an issue starting or verifying the workflow.")

if __name__ == "__main__":
    main()