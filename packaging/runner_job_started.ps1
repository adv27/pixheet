<#
.SYNOPSIS
Refuse each job on the self-hosted runner that is not a trusted build of Pixheet.exe.

.DESCRIPTION
The runner starts this script before each job. The runner runs the job only when the
script stops with exit code 0. Set the script in the .env file of the runner:

    ACTIONS_RUNNER_HOOK_JOB_STARTED=C:\actions-runner-hooks\runner_job_started.ps1

The script lets a job run only when all these conditions are true:
  1. The repository is $Repository.
  2. The event is "push" or "workflow_dispatch".
  3. The workflow is .github/workflows/release.yml from the branch "master" or from a
     tag "v*".

A pull request from a fork can change each workflow file. Thus the runner itself must
refuse the jobs that a pull request starts. This script does that. Read
packaging/README.md for the full procedure.

The script runs on Windows PowerShell 5.1 and on PowerShell 7.
#>

# Change this value only when the repository gets a new owner or a new name.
$Repository = 'adv27/pixheet'

$workflow = "$Repository/.github/workflows/release.yml"
$eventName = $env:GITHUB_EVENT_NAME
$workflowRef = $env:GITHUB_WORKFLOW_REF

$trustedRepository = $env:GITHUB_REPOSITORY -ceq $Repository
$trustedEvent = @('push', 'workflow_dispatch') -ccontains $eventName
$trustedWorkflow = ($workflowRef -ceq "$workflow@refs/heads/master") -or
    ($workflowRef -clike "$workflow@refs/tags/v*")

if ($trustedRepository -and $trustedEvent -and $trustedWorkflow) {
    Write-Host "runner_job_started: run the job. Event '$eventName', workflow '$workflowRef'."
    exit 0
}

Write-Host "runner_job_started: refuse the job. This runner runs only release.yml of $Repository, from a push or a manual start on master or on a v* tag."
Write-Host "runner_job_started: repository '$env:GITHUB_REPOSITORY', event '$eventName', workflow '$workflowRef'."
exit 1
