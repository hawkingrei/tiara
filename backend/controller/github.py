from http import HTTPStatus
from backend.model import base
from backend.tool.logger import get_logger
from github_webhook import Webhook
from backend import config
from backend.model.issue import ISSUE_TABLE_NAME, Issue
from backend.model.init_database import diff_and_get_changed_fields, PROTECTED_FIELDS
from backend.tool.send_issue_comment import search_similar_issues, log_similar_issues, send_issue_comment, should_send_comment

logger = get_logger(__name__)

webhook = None

def init_webhook(app):
    global webhook
    webhook = Webhook(
        app,
        endpoint=config.GITHUB_WEBHOOK_URL,
        secret=config.GITHUB_WEBHOOK_SECRET
    )

    @webhook.hook(event_type="issues")
    def github_webhook_issues(data):
        """Handle GitHub Issues webhook events."""
        logger.info(f"Received GitHub Issues webhook: {data}")
        
        try:
            # Convert webhook payload to our Issue model
            issue = Issue.from_webhook_payload(data)
            
            action = data.get('action', 'unknown')
            logger.info(f"Issue {action}: #{issue.github_issue_number} - {issue.title}")
            logger.info(f"Repository: {issue.repository_name}")
            logger.info(f"Author: {issue.author_login}")
            logger.info(f"State: {issue.state}")
            
            label_names = []
            if issue.labels:
                labels = issue.get_labels_list()
                label_names = [label['name'] for label in labels]
                logger.info(f"Labels: {label_names}")
            
            if issue.assignees:
                assignees = issue.get_assignees_list()
                logger.info(f"Assignees: {[assignee['login'] for assignee in assignees]}")
            
            # Save issue to database first
            should_reply = save_issue_to_database(issue, action)
            
            # Skip if not reply all and issue label doesn't contain reply label
            if not should_reply:
                logger.info(f"Skipping reply for issue #{issue.github_issue_number}")
                return {'status': 'success', 'message': 'Issue skipped (not marked for reply)'}, HTTPStatus.OK

            # Perform semantic search for similar issues
            try:
                logger.info(f"Searching for similar issues to #{issue.github_issue_number}")
                similar_issues = search_similar_issues(issue, limit_per_field=10)
                log_similar_issues(similar_issues, issue)
            except Exception as e:
                logger.error(f"Error during similarity search for issue #{issue.github_issue_number}: {str(e)}")
                # Don't fail the webhook if similarity search fails
            
            logger.info(f"Successfully processed {action} event for issue #{issue.github_issue_number}")
            
            # Send comment to issue
            if should_send_comment(action, issue, similar_issues):
                send_issue_comment(issue, similar_issues)
            
        except Exception as e:
            logger.error(f"Error processing Issues webhook: {str(e)}")
            return {'status': 'error', 'message': str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR
        
        return {'status': 'success', 'message': 'Issues webhook processed'}, HTTPStatus.OK


def save_issue_to_database(issue: Issue, action: str):
    """
    Save or update issue in database with diff optimization.
    
    Args:
        issue: Issue model instance
        action: The webhook action (opened, edited, closed, etc.)
    Returns:
        bool: Whether the REPLY_LABEL is present (on creation) or newly added (on update), indicating the bot should reply.
    """
    logger.info(f"Saving issue to database: {issue.github_issue_id} (action: {action})")

    table = base.db.open_table(ISSUE_TABLE_NAME)
    
    # Indicates whether the bot should reply for this issue event
    should_reply = False
    
    try:
        if action == 'opened':
            # Insert new issue
            table.insert(issue)
            logger.info(f"Inserted new issue #{issue.github_issue_number}")
            
            # Determine if the REPLY_LABEL is present on newly opened issues
            labels_list = issue.get_labels_list() if issue.labels else []
            if any(label.get('name') == config.REPLY_LABEL for label in labels_list):
                should_reply = True
        else:
            # Update existing issue with diff optimization
            existing_issue = table.get(issue.github_issue_id)
            
            # Determine if the REPLY_LABEL was newly added before any field update logic
            current_has_reply = False
            if issue.labels:
                current_has_reply = any(label.get('name') == config.REPLY_LABEL for label in issue.get_labels_list())
            db_has_reply = False
            if existing_issue and getattr(existing_issue, 'labels', None):
                db_has_reply = any(label.get('name') == config.REPLY_LABEL for label in existing_issue.get_labels_list())
            if current_has_reply and not db_has_reply:
                should_reply = True
            
            if existing_issue:
                logger.info(f"Checking for changes in issue #{issue.github_issue_number} (action: {action})")
                
                # Get only changed fields
                changed_fields = diff_and_get_changed_fields(existing_issue, issue)
                
                if changed_fields:
                    logger.info(f"Updating {len(changed_fields)} changed fields for issue #{issue.github_issue_number}")
                    logger.debug(f"Changed fields: {list(changed_fields.keys())}")
                    
                    table.update(
                        changed_fields,
                        {"github_issue_id": issue.github_issue_id}
                    )
                    logger.info(f"Updated existing issue #{issue.github_issue_number}")
                else:
                    logger.info(f"No changes detected for issue #{issue.github_issue_number}, skipping update")
            else:
                # Issue doesn't exist, insert it
                table.insert(issue)
                logger.info(f"Inserted new issue #{issue.github_issue_number} (not found for update)")
                
    except Exception as e:
        logger.error(f"Error saving issue #{issue.github_issue_number}: {str(e)}")
        raise

    return should_reply
