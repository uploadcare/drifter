from logzero import logger
import logging
import logzero
from urllib.parse import urlparse
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
from concurrent.futures.thread import ThreadPoolExecutor
import re
import json
import time
import requests
import signal
import os
# import boto3
import uuid
import subprocess
import settings

requested_to_quit = False
last_summary_emitted = 0

endpoint_definitions = None
alert_definitions = None
metrics_definitions = None


import sys

def main():
    logger.info("starting...")

    setup_signal_handling()

    repo_folder = settings.TERRAFORM_LOCAL_PATH

    # get terraform version from state found with s3 bucket/key
    # terraform_version = get_terraform_version(settings.TERRAFORM_S3_BUCKET, settings.TERRAFORM_S3_KEY)
    terraform_version = get_local_terraform_version(repo_folder)

    # install appropriate terraform version
    terraform_bin = install_terraform(terraform_version)

    # get current head of terraform repository
    # fetch that version as an archive and unzip it
    # repo_folder = fetch_current_repo_head()

    # terraform init (with parameters)
    if not terraform_initialise(terraform_bin, repo_folder):
        sys.exit(1)

    # terraform plan (with parameters)
    metrics = terraform_plan(terraform_bin, repo_folder)
    if metrics is None:
        sys.exit(1)

    # ship metrics
    ship_metrics_to_console(metrics)

    report_to_teamcity(metrics)

    if settings.CLOUDWATCH_NAMESPACE:
        ship_metrics_to_cloudwatch(metrics)

    if settings.SLACK_WEBHOOK_URL and deduplicate_alert(metrics):
        alert_slack(pretty_print_metrics(metrics))

    sys.exit(metrics['terraform_status'])


def signal_handler(signum, frame):
    logger.info(f"Caught signal {signum}")
    global requested_to_quit
    requested_to_quit = True


def setup_signal_handling():
    logger.info("setting up signal handling")
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)


def get_file_or_s3(uri):
    logger.info(f"getting file URI {uri}")

    if uri.lower().startswith("s3://"):
        s3 = boto3.resource("s3")
        parse_result = urlparse(uri)
        s3_object = s3.Object(parse_result.netloc, parse_result.path.lstrip("/"))
        return s3_object.get()["Body"].read().decode("utf-8")

    return open(uri).read()


def download_file(url, filename, headers={}):
    with requests.get(url, stream=True, headers=headers) as r:
        r.raise_for_status()
        with open(filename, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)


def get_terraform_version(bucket, key):
    logger.info(f"getting Terraform version from remote state at s3://{bucket}/{key}")

    remote_state = json.loads(get_file_or_s3(f"s3://{bucket}/{key}"))

    version = remote_state["terraform_version"]
    logger.debug(f"terraform version = {version}")

    return version


def get_local_terraform_version(path):
    if settings.TERRAFORM_GITHUB_FOLDER:
        path = os.path.join(path, settings.TERRAFORM_GITHUB_FOLDER)

    with open(os.path.join(path, 'terraform.tfstate')) as fp:
        remote_state = json.load(fp)

    version = remote_state["terraform_version"]
    logger.debug(f"terraform version = {version}")

    return version


def install_terraform(version):
    logger.info(f"installing Terraform version {version}")

    filename = f"terraform_{version}_linux_amd64"
    url = f"https://releases.hashicorp.com/terraform/{version}/{filename}.zip"
    zip = f"{settings.TMP_FOLDER}/{filename}.zip"
    out_path = f"{settings.TMP_FOLDER}/{filename}"
    bin = f"{out_path}/terraform"

    if os.path.exists(bin):
        return bin

    logger.debug(f"downloading Terraform from {url}")
    download_file(url=url, filename=zip)

    logger.debug(f"making output directory {out_path}")
    os.mkdir(out_path)

    logger.debug(f"unzipping archive {zip} to {out_path}")
    zip_output = subprocess.Popen(
        f"unzip -o {zip} -d {out_path}",
        cwd=settings.TMP_FOLDER,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    ).stderr.read()

    logger.debug(f"zip stderr output was: {zip_output}")

    return bin


def fetch_current_repo_head():
    logger.info(f"getting current HEAD of {settings.TERRAFORM_GITHUB_REPO}")

    api_url = f"https://api.github.com/repos/{settings.TERRAFORM_GITHUB_REPO}/branches/{settings.TERRAFORM_GITHUB_BRANCH}"

    r=requests.get(api_url, headers={
        "Authorization": f"token {settings.TERRAFORM_GITHUB_TOKEN}",
        "User-Agent": f"Drifter (Terraform monitor)"
    })

    parsed_json = json.loads(r.text)

    repo_sha = parsed_json["commit"]["sha"]

    logger.info(f"commit SHA was {repo_sha}")

    zip = f"{settings.TMP_FOLDER}/repo.zip"
    out_path = f"{settings.TMP_FOLDER}/repo"

    modified_repo_name = settings.TERRAFORM_GITHUB_REPO.replace("/", "-")
    full_repo_path = f"{out_path}/{modified_repo_name}-{repo_sha}"

    if os.path.isdir(full_repo_path):
        logger.info(f"skipping download as it already exists in {settings.TMP_FOLDER}")
    else:
        api_url = f"https://api.github.com/repos/{settings.TERRAFORM_GITHUB_REPO}/zipball/{repo_sha}"

        logger.debug(f"downloading repo from {api_url}")

        download_file(url=api_url, filename=zip, headers={
            "Authorization": f"token {settings.TERRAFORM_GITHUB_TOKEN}",
            "User-Agent": f"Drifter (Terraform monitor)"
        })

        logger.debug(f"making output directory {out_path}")
        os.mkdir(out_path)

        logger.debug(f"unzipping archive {zip} to {out_path}")
        zip_output = subprocess.Popen(
            f"unzip -o {zip} -d {out_path}",
            cwd=settings.TMP_FOLDER,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        ).stderr.read()

        logger.debug(f"zip stderr output was: {zip_output}")

    return full_repo_path


def terraform_initialise(terraform_bin, repo_folder):
    logger.info(f"initialising Terraform ({terraform_bin})")

    candidate_folder = repo_folder
    if settings.TERRAFORM_GITHUB_FOLDER:
        candidate_folder = f"{candidate_folder}/{settings.TERRAFORM_GITHUB_FOLDER}"
        logger.info(f"using candidate repo folder {candidate_folder}")

    child = subprocess.Popen(
        f"{terraform_bin} init -input=false -no-color",
        cwd=candidate_folder,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    init_output = get_utf8(child.stdout.read())
    init_error = get_utf8(child.stderr.read())

    if len(init_error) > 0:
        logger.info(f"terraform init failed. output was: {init_output}")
        logger.info(f"error was: {init_error}")

        alert_slack(f"problem during initialise: {init_error}")
        return False

    logger.debug(f"terraform init output was: {init_output}")
    return True


def terraform_plan(terraform_bin, repo_folder):
    logger.info(f"planning Terraform ({terraform_bin}) using {repo_folder}")

    candidate_folder = repo_folder
    if settings.TERRAFORM_GITHUB_FOLDER:
        candidate_folder = f"{candidate_folder}/{settings.TERRAFORM_GITHUB_FOLDER}"
        logger.info(f"using candidate repo folder {candidate_folder}")

    plan_start_time = time.time()

    child = subprocess.Popen(
        f"{terraform_bin} plan --detailed-exitcode -input=false -lock=false -no-color",
        cwd=candidate_folder,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True
    )

    stdout, stderr = child.communicate()

    exit_code = child.poll()

    plan_time_taken = time.time() - plan_start_time

    plan_output = get_utf8(stdout)
    plan_error = get_utf8(stderr)

    if exit_code == 1:
        # plan failed
        logger.info(f"terraform plan failed. output was: {plan_output}")
        logger.info(f"error was: {plan_error}")
        return None
    else:
        # plan finished
        logger.debug(f"terraform plan output was: {plan_output}")

        logger.info(f"plan finished")

        resource_count = 0
        pending_add = 0
        pending_change = 0
        pending_destroy = 0
        pending_total = 0

        resource_regex = re.compile(r".*Refreshing state\.\.\.")
        plan_regex = re.compile(r".*Plan: (\d+) to add, (\d+) to change, (\d+) to destroy\.")

        for plan_line in plan_output.split("\n"):
            # count number of resources
            if resource_regex.match(plan_line):
                resource_count = resource_count + 1

            m = plan_regex.match(plan_line)
            if m:
                pending_add = int(m.group(1))
                pending_change = int(m.group(2))
                pending_destroy = int(m.group(3))

                logger.debug(f"line: {plan_line}")
                logger.debug(f"pending add: {pending_add}")
                logger.debug(f"pending change: {pending_change}")
                logger.debug(f"pending destroy: {pending_destroy}")

        pending_total = pending_add + pending_change + pending_destroy

        # logger.debug(f"pending total: {pending_total}")

        # logger.debug(f"plan time taken: {plan_time_taken}")
        # logger.debug(f"terraform_status: {exit_code}")

    return {
        "terraform_status": exit_code,
        "resource_count": resource_count,
        "pending_add": pending_add,
        "pending_change": pending_change,
        "pending_destroy": pending_destroy,
        "pending_total": pending_total,
        "plan_time": plan_time_taken
    }


def get_utf8(input):
    try:
        input = input.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        pass

    return input


def ship_metrics_to_console(metrics):
    logger.info(f"ship_metrics_to_console")

    message = pretty_print_metrics(metrics)
    logger.info(message)


def ship_metrics_to_cloudwatch(metrics):
    logger.info(f"shipping metrics to cloudwatch")
    cloudwatch = boto3.client("cloudwatch", settings.AWS_REGION)

    cloudwatch.put_metric_data(
        MetricData=[
            {
                "MetricName": "Pending-Add",
                "Dimensions": [
                    {
                        "Name": "GitHubRepo",
                        "Value": settings.TERRAFORM_GITHUB_REPO
                    }
                ],
                "Unit": "Count",
                "Value": metrics["pending_add"]
            },
            {
                "MetricName": "Pending-Change",
                "Dimensions": [
                    {
                        "Name": "GitHubRepo",
                        "Value": settings.TERRAFORM_GITHUB_REPO
                    }
                ],
                "Unit": "Count",
                "Value": metrics["pending_change"]
            },
            {
                "MetricName": "Pending-Destroy",
                "Dimensions": [
                    {
                        "Name": "GitHubRepo",
                        "Value": settings.TERRAFORM_GITHUB_REPO
                    }
                ],
                "Unit": "Count",
                "Value": metrics["pending_destroy"]
            },
            {
                "MetricName": "Pending-Total",
                "Dimensions": [
                    {
                        "Name": "GitHubRepo",
                        "Value": settings.TERRAFORM_GITHUB_REPO
                    }
                ],
                "Unit": "Count",
                "Value": metrics["pending_total"]
            }
        ],
        Namespace=settings.CLOUDWATCH_NAMESPACE
    )


def deduplicate_alert(metrics):
    logger.info(f"deduplicating alert")
    return True


def alert_slack(message):
    if not settings.SLACK_WEBHOOK_URL:
        return

    logger.info(f"alerting to Slack")

    _ = requests.post(settings.SLACK_WEBHOOK_URL, json={"text": message, "link_names": 1})


def get_relative_time(start_time, end_time):
    return relativedelta(microsecond=int(round((end_time-start_time) * 1000000)))


def pretty_print_metrics(metrics):
    logger.info(f"pretty_print_metrics")

    attrs = ["years", "months", "days", "hours", "minutes", "seconds", "microsecond"]
    human_readable = lambda delta: ["%d %s" % (getattr(delta, attr), getattr(delta, attr) > 1 and attr or attr[:-1])
        for attr in attrs if getattr(delta, attr)]

    pending_message = f'Repository: {settings.TERRAFORM_GITHUB_REPO}, Branch: {settings.TERRAFORM_GITHUB_BRANCH}, Folder: {settings.TERRAFORM_GITHUB_FOLDER}\nPending: Add={metrics["pending_add"]}, Change={metrics["pending_change"]}, Destroy={metrics["pending_destroy"]}, Total={metrics["pending_total"]}'

    changes_message = "No changes detected."

    if metrics["terraform_status"] == 2:
        changes_message = f"Drift detected! {pending_message}"

    resources_message = f'Resource count: {metrics["resource_count"]}'

    delta = relativedelta(seconds=metrics["plan_time"])

    time_taken_human_readable = ", ".join(human_readable(delta))
    timing_message = f"Plan took {time_taken_human_readable}."

    return f"{changes_message}\n{resources_message}\n{timing_message}"


TEAMCITY_METRICS = [
    ('drift_total', 'pending_total'),
    ('drift_add', 'pending_add'),
    ('drift_change', 'pending_change'),
    ('drift_destroy', 'pending_destroy'),
    ('resource_count', 'resource_count'),
]

def report_to_teamcity(metrics):
    if metrics["terraform_status"] == 2:
        sys.stdout.write("##teamcity[buildProblem description='Drift detected']\n")

    for tc_key, m_key in TEAMCITY_METRICS:
        sys.stdout.write("##teamcity[buildStatisticValue key='{}' value='{}']\n".format(tc_key, metrics[m_key]))


if __name__ == "__main__":
    if settings.DEBUG:
        logzero.loglevel(logging.DEBUG)
    else:
        logzero.loglevel(logging.INFO)

    main()
