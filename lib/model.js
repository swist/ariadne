'use strict'

class Model {
  labels(){
    let res = []
    let proto = Object.getPrototypeOf(this)

    while (proto.constructor.name !== 'Model'){
      res.push(proto.constructor.name)
      proto = Object.getPrototypeOf(proto)
    }

    return res
  }
}

export default Model
