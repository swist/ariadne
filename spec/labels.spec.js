'use strict'
import "babel-polyfill"
import Model from '../lib/model'


class Bar extends Model {}
class Foo extends Bar {}


describe('Model', function () {
  it('should return the list of labels for the prototype chain', () => {
    let labels = Foo.labels()
    expect(labels).toEqual(['Foo', 'Bar'])
  })
})
