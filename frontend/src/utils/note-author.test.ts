import { expect } from '@open-wc/testing';

import {
  noteAuthorIcon,
  noteAuthorKind,
  noteAuthorLabel,
  noteAuthorMarker,
  noteAuthorName,
} from './note-author';

describe('note author markers', () => {
  it('separates an agent author from a human one', () => {
    expect(noteAuthorKind('agent')).to.equal('agent');
    expect(noteAuthorKind('AGENT')).to.equal('agent');
    expect(noteAuthorKind('jwt')).to.equal('human');
    expect(noteAuthorKind('api_key')).to.equal('human');
    expect(noteAuthorKind(null)).to.equal('unknown');
  });

  it('names the credential kind behind the author', () => {
    expect(noteAuthorMarker('agent')).to.equal('agent');
    expect(noteAuthorMarker('api_key')).to.equal('API key');
    expect(noteAuthorMarker('jwt')).to.equal('person');
    expect(noteAuthorMarker('session')).to.equal('person');
  });

  it('states an unrecognised credential rather than assuming a person', () => {
    expect(noteAuthorMarker('something_new')).to.equal('unknown');
    expect(noteAuthorMarker(undefined)).to.equal('unknown');
    expect(noteAuthorKind('something_new')).to.equal('unknown');
    expect(noteAuthorKind(undefined)).to.equal('unknown');
  });

  it('gives the kinds different icons', () => {
    expect(noteAuthorIcon('agent')).to.equal('robot');
    expect(noteAuthorIcon('jwt')).to.equal('person');
    expect(noteAuthorIcon(null)).to.equal('question-circle');
    expect(noteAuthorIcon('something_new')).to.equal('question-circle');
  });

  it('falls back to an honest name when the row stored none', () => {
    expect(noteAuthorName('  Jane Doe  ')).to.equal('Jane Doe');
    expect(noteAuthorName('')).to.equal('Unknown author');
    expect(noteAuthorName(null)).to.equal('Unknown author');
  });

  it('reads as one line on an indicator', () => {
    expect(noteAuthorLabel('Reviewer', 'agent')).to.equal('Reviewer (agent)');
    expect(noteAuthorLabel('Jane Doe', 'jwt')).to.equal('Jane Doe (person)');
  });
});
