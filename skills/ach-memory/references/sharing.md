# Sharing a project's memory

A project's memory belongs to the users and groups on its access list. Everyone on it is a
co-owner: they read, write, share, unshare and rename, and can remove anyone else, the person
who created it included. Personal memory is never shared.

## When the user asks to share

"Share this project with Joan Pau", "give the dream team access":

1. Find each person's email. Given only a name, look it up with a contacts or directory tool
   if the host has one; otherwise ask. Never guess an address.
2. For a team, call `whoami`: you can only share with a group it lists. A team that is not
   there does not reach ach-memory yet; tell the user it has to be added to the ACH `grants`
   Environment first.
3. Call `share_project` with `users` and/or `groups`.
4. Answer from what it returns: "Done: Joan Pau now has access, with you and team dream."

"Stop sharing with X" is `unshare_project`; the last entry cannot be removed. "Who can see
this project?" is `list_access`.

Never share or unshare on your own initiative.

## From the shell

```bash
ach-memory whoami                                   # you and your groups
ach-memory share                                    # who has access
ach-memory share user joanpau.desola@ackstorm.com   # one or more emails
ach-memory share group dream                        # one or more of your groups
ach-memory unshare user joanpau.desola@ackstorm.com
ach-memory unshare group dream
```

`--project <slug>` names another project; otherwise it is the current folder's.
