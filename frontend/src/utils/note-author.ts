/**
 * Who wrote a note, and with what credential.
 *
 * A note steers a running agent, so the author is part of the audit trail,
 * not decoration. Since an agent can write one too, every surface that shows
 * a note has to make "a person said this" and "another agent said this"
 * visibly different; this module is the one place that decides how, so the
 * sessions list and the note list cannot drift into two vocabularies.
 *
 * The credential kind is stamped server side (`author_auth_method`) and the
 * sender never chooses it, which is what makes it worth showing at all.
 */

/** The credential an agent author uses, as the server stamps it. */
export const AGENT_AUTH_METHOD = 'agent';

export type NoteAuthorKind = 'agent' | 'human';

/** Agent or person: the distinction the marker has to carry. */
export function noteAuthorKind(
  authMethod: string | null | undefined
): NoteAuthorKind {
  return (authMethod || '').trim().toLowerCase() === AGENT_AUTH_METHOD
    ? 'agent'
    : 'human';
}

/** Short marker beside the name, naming the credential kind. */
export function noteAuthorMarker(
  authMethod: string | null | undefined
): string {
  switch ((authMethod || '').trim().toLowerCase()) {
    case AGENT_AUTH_METHOD:
      return 'agent';
    case 'api_key':
      return 'API key';
    case 'jwt':
    case 'session':
      return 'person';
    default:
      // An unknown credential is stated, not quietly rendered as a person:
      // the marker exists to keep authorship honest.
      return 'unknown';
  }
}

/** Icon for the marker, so the two kinds differ at a glance, not only in text. */
export function noteAuthorIcon(authMethod: string | null | undefined): string {
  return noteAuthorKind(authMethod) === 'agent' ? 'robot' : 'person';
}

/** The author's name, falling back to something honest when none was stored. */
export function noteAuthorName(display: string | null | undefined): string {
  return (display || '').trim() || 'Unknown author';
}

/** One line naming the author and the credential kind: "Reviewer (agent)". */
export function noteAuthorLabel(
  display: string | null | undefined,
  authMethod: string | null | undefined
): string {
  return `${noteAuthorName(display)} (${noteAuthorMarker(authMethod)})`;
}
